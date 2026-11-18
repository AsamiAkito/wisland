# -*- coding: utf-8 -*-
"""Bounded vault maintenance: archived sources, routed notes and cited topic drafts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path

from .agent_worker import CATEGORIES, TOPICS
from .core import _write_note, atomic_write, inside, readable_path, safe_name
from .organizer_cloud import call_agent

TOPIC_FORMAT = 2


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wiki(path, title=""):
    path = str(path).replace("\\", "/").removesuffix(".md")
    if any(c in path for c in "[]|\n"):
        raise ValueError("链接路径无效")
    title = str(title).replace("[", "（").replace("]", "）").replace("|", "／").replace("\n", " ")
    return "[[" + path + ("|" + title if title else "") + "]]"


def validate_analysis(value):
    if value.get("category") not in CATEGORIES or value.get("topic") not in TOPICS:
        raise ValueError("Agent 返回未知分类")
    if type(value.get("sufficient")) is not bool:
        raise ValueError("Agent 未判断资料是否足够")
    for name in ("title", "summary"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError("Agent 缺少标题或摘要")
    for name in ("points", "tags"):
        if not isinstance(value.get(name), list) or not all(isinstance(v, str) for v in value[name]):
            raise ValueError("Agent 要点或标签格式错误")


class Organizer:
    def __init__(self, store, vault, respect_privacy=False):
        self.store = store
        self.vault = Path(vault).resolve()
        self.respect_privacy = respect_privacy
        self.path = store.root / "organizer-ledger.json"
        self.ledger = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"notes": {}, "topics": {}, "events": []}
        )
        for entry in self.ledger["notes"].values():
            row = store.get(entry["id"])
            if row and row["result"]:
                entry.setdefault(
                    "metadata_version",
                    json.loads(row["result"]).get("metadata_version", row["metadata_version"]),
                )

    def save(self):
        payload = json.dumps(self.ledger, ensure_ascii=False, indent=2).encode("utf-8")
        if not self.path.exists() or self.path.read_bytes() != payload:
            atomic_write(self.path, payload)

    def archive(self, source, expected_hash):
        path = inside(self.vault, source)
        if not path.exists():
            raise ValueError("待归档文件不存在")
        if file_hash(path) != expected_hash:
            raise ValueError("原记录已被修改，跳过此条以保护新内容")
        target_rel = "09 Archive/Agent原始收集/" + safe_name(path.stem, 110) + "-" + expected_hash[:8] + ".md"
        target = inside(self.vault, target_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_hash(target) != expected_hash:
                raise ValueError("归档目标已有不同内容")
            path.unlink()
        else:
            path.rename(target)
        self.ledger["events"].append(
            {
                "action": "archive",
                "from": source,
                "to": target_rel,
                "hash": expected_hash,
                "time": time.time(),
            }
        )
        self.save()
        return target_rel

    def apply(self, row, analysis, sources=None):
        with self.store.lock:
            current = self.store.get(row["id"])
            if not current or (self.respect_privacy and current["disposition"] != "active"):
                raise ValueError("资料已暂停或移除")
            if current["metadata_version"] != row["metadata_version"]:
                raise ValueError("资料说明已改变，延后整理")
            return self._apply(current, analysis, sources)

    def _apply(self, row, analysis, sources=None):
        validate_analysis(analysis)
        material = row["digest"]
        prior = self.ledger["notes"].get(material)
        if prior and prior["path"] and inside(self.vault, prior["path"]).exists():
            if file_hash(inside(self.vault, prior["path"])) != prior["hash"]:
                raise ValueError("托管笔记已被手动修改，保留人工版本")
        if row["editing"] or row.get("draft"):
            raise ValueError("资料正在编辑，延后整理")
        result = dict(analysis)
        original_result = json.loads(row["result"]) if row["result"] else {}
        if original_result.get("source_url"):
            result["source_url"] = original_result["source_url"]
        result.setdefault(
            "review_notes", original_result.get("review_notes") or (prior or {}).get("review_notes", [])
        )
        if original_result.get("existing_attachment"):
            result.setdefault("existing_attachment", original_result["existing_attachment"])
        if original_result.get("attachment_aliases"):
            result.setdefault("attachment_aliases", original_result["attachment_aliases"])
        result.update(
            status="processed" if analysis["sufficient"] else "source_only",
            organized=True,
            source_notes=list(dict.fromkeys((prior or {}).get("sources", []) + list(sources or []))),
            warning=("AI 自动整理，事实与结论尚未经本人核验。" + str(analysis.get("reason") or "")),
            metadata_version=row["metadata_version"],
        )
        attachments = analysis.get("attachments", [])
        if attachments:
            candidate = inside(self.vault, attachments[0])
            if candidate.is_file():
                result["existing_attachment"] = attachments[0]
        if prior:
            target_rel = prior["path"]
        else:
            folder = (
                "05 Resources/" + analysis["category"] if analysis["sufficient"] else "00 Inbox收件箱/待补充"
            )
            target_rel = readable_path(self.vault, folder, analysis["title"], ".md", capture_id=row["id"])
        self.store.update(
            row["id"],
            result=json.dumps(result, ensure_ascii=False),
            note_path=target_rel,
            note_hash=prior["hash"]
            if prior
            else (row["note_hash"] if target_rel == row["note_path"] else ""),
            state="organizing",
            stage="Agent 自动归档",
            progress=-1,
        )
        current = self.store.get(row["id"])
        # This call is limited to the explicitly authorized organizer operation.
        # It keeps a pre-existing local-only disposition unchanged.
        note = _write_note(self.store, current, self.vault)
        entry = {
            "id": row["id"],
            "path": target_rel,
            "title": result["title"],
            "summary": result["summary"],
            "points": result["points"],
            "tags": result["tags"],
            "category": result["category"],
            "topic": result["topic"],
            "sufficient": result["sufficient"],
            "sources": result["source_notes"],
            "hash": file_hash(note),
            "updated": time.time(),
            "metadata_version": row["metadata_version"],
            "review_notes": result["review_notes"],
            "related_notes": (prior or {}).get("related_notes", []),
            "index_only": bool(result.get("index_only")),
            "coverage": result.get("coverage", {}),
            "purpose": result.get("purpose", ""),
        }
        entry["analysis_hash"] = semantic_hash(entry)
        self.ledger["notes"][material] = entry
        self.ledger["events"].append(
            {"action": "write", "path": target_rel, "hash": entry["hash"], "time": time.time()}
        )
        self.save()
        return entry

    def topic_groups(self, include_paused=False):
        groups = {}
        active = (
            {
                r["digest"]
                for r in self.store.rows()
                if r["disposition"] == "active" and not r["draft"] and not r["editing"]
            }
            if self.respect_privacy and not include_paused
            else None
        )
        for material, entry in self.ledger["notes"].items():
            note = inside(self.vault, entry["path"])
            if entry["sufficient"] and note.exists() and (active is None or material in active):
                groups.setdefault(entry["topic"], []).append(entry)
        return groups

    def update_topics(self, config, cloud=call_agent, should_stop=lambda: False):
        # Migrate fingerprints from file bytes to actual synthesis input, so adding
        # navigation links does not cause an endless topic regeneration loop.
        for topic, entries in self.topic_groups().items():
            if should_stop():
                return
            prior = self.ledger["topics"].get(topic)
            old = hashlib.sha256(
                json.dumps([(e["path"], e["hash"]) for e in entries], sort_keys=True).encode()
            ).hexdigest()
            for entry in entries:
                entry["analysis_hash"] = semantic_hash(entry)
            if prior and prior["fingerprint"] == old:
                prior["fingerprint"] = hashlib.sha256(
                    json.dumps([(e["path"], e["analysis_hash"]) for e in entries], sort_keys=True).encode()
                ).hexdigest()
        for topic, entries in self.topic_groups().items():
            if should_stop():
                return
            if len(entries) < 2 or topic == "其他资料":
                continue
            fingerprint = hashlib.sha256(
                json.dumps([(e["path"], e["analysis_hash"]) for e in entries], sort_keys=True).encode()
            ).hexdigest()
            prior = self.ledger["topics"].get(topic)
            rel = "04 Knowledge/AI专题/" + safe_name(topic) + ".md"
            target = inside(self.vault, rel)
            before_hash = file_hash(target) if target.exists() else None
            if prior and target.exists() and file_hash(target) != prior["hash"]:
                continue  # User edits take precedence over automatic synthesis.
            if (
                prior
                and prior["fingerprint"] == fingerprint
                and prior.get("format") == TOPIC_FORMAT
                and target.exists()
            ):
                continue
            sources = [
                {
                    "id": f"S{i + 1}",
                    "title": e["title"],
                    "summary": e["summary"],
                    "points": e["points"][:8],
                    "review_notes": e.get("review_notes", []),
                    "index_only": bool(e.get("index_only")),
                    "coverage": e.get("coverage", {}),
                }
                for i, e in enumerate(entries)
            ]
            links = {f"S{i + 1}": wiki(e["path"], f"〔{i + 1}〕") for i, e in enumerate(entries)}
            try:
                value = cloud(config, {"mode": "synthesize", "topic": topic, "sources": sources})
                body = value.get("body")
                if isinstance(body, str) and (
                    len(body) > 1800 or len(re.findall(r"^#{2,6} ", body, re.M)) > 4
                ):
                    value = cloud(
                        config,
                        {
                            "mode": "synthesize",
                            "topic": topic,
                            "sources": sources,
                            "revision": "上一版超过长度或章节限制。重写600到800字、最多3节；只提炼可复用结论和条件，不逐篇列摘要，不写长代码，不展开工具清单。每段保留来源编号，总长度须小于1400字符。",
                        },
                    )
                    body = value.get("body")
                markers = set(re.findall(r"\[(S\d+)\]", body or ""))
                if not isinstance(body, str) or not markers or not markers <= links.keys():
                    raise ValueError("专题来源编号不完整")
                if "[[" in body or re.search(r"\]\(https?://", body):
                    raise ValueError("专题包含未授权链接")
                body = re.sub(r"^# .+\n?", "", body, flags=re.M).strip()
                if len(body) > 1800 or len(re.findall(r"^#{2,6} ", body, re.M)) > 4:
                    raise ValueError("专题过长，改用简短资料入口")
                bare = set(re.findall(r"\bS\d+\b", body))
                if not bare <= links.keys():
                    raise ValueError("专题含未知来源编号")
                body = re.sub(r"\[(S\d+)\]", lambda m: links[m[1]], body)
                # Bare source IDs from older prompts become readable concept names.
                body = re.sub(r"\bS(\d+)\b", lambda m: entries[int(m[1]) - 1]["title"], body)
            except Exception:
                body = "## 资料速览\n\n" + "\n".join(
                    "- " + wiki(e["path"], e["title"]) + "：" + e["summary"][:100] for e in entries
                )
            text = (
                "---\ntype: knowledge_ai_draft\nagent_managed: true\nreview: unverified\n---\n\n# "
                + topic
                + "\n\n> AI 综合草稿，基于已有资料；不代表本人已理解或验证。\n\n"
                + body
            )
            text += (
                "\n\n## 来源清单\n\n"
                + "\n".join(f"{i + 1}. " + wiki(e["path"], e["title"]) for i, e in enumerate(entries))
                + "\n"
            )
            current = self.topic_groups().get(topic, [])
            if should_stop():
                return
            if [e["path"] for e in current] != [e["path"] for e in entries]:
                continue
            if target.exists() and (before_hash is None or file_hash(target) != before_hash):
                continue
            atomic_write(target, text.encode("utf-8"))
            self.ledger["topics"][topic] = {
                "path": rel,
                "hash": file_hash(target),
                "fingerprint": fingerprint,
                "count": len(entries),
                "format": TOPIC_FORMAT,
            }
            self.save()
        if config.get("organizer_enabled"):
            self.update_relations(config, cloud, should_stop)
        self.write_index()
        self.connect_topics()

    def update_relations(self, config, cloud=call_agent, should_stop=lambda: False):
        # ponytail: bounded context; use candidate retrieval if a topic exceeds this window.
        entries = [e for group in self.topic_groups().values() for e in group][-80:]
        if len(entries) < 2 or should_stop():
            return
        fingerprint = hashlib.sha256(
            json.dumps([(e["path"], semantic_hash(e)) for e in entries], sort_keys=True).encode()
        ).hexdigest()
        prior = self.ledger.get("relations", {})
        if prior.get("fingerprint") == fingerprint and (
            prior.get("done") or prior.get("retry_at", 0) > time.time()
        ):
            return
        sources = {f"N{i + 1}": e for i, e in enumerate(entries)}
        try:
            payload = [
                {
                    "id": key,
                    "title": e["title"],
                    "topic": e["topic"],
                    "summary": e["summary"][:300],
                    "points": e["points"][:3],
                }
                for key, e in sources.items()
            ]
            result = cloud(config, {"mode": "relate", "sources": payload})
            edges = result.get("edges")
            if not isinstance(edges, list) or len(edges) > 40:
                raise ValueError("关联列表格式错误")
            related = {e["path"]: [] for e in entries}
            seen = set()
            for edge in edges:
                a, b, reason = edge.get("a"), edge.get("b"), edge.get("reason")
                if (
                    a not in sources
                    or b not in sources
                    or a == b
                    or not isinstance(reason, str)
                    or not 1 <= len(reason) <= 80
                    or any(c in reason for c in "[]\n")
                ):
                    raise ValueError("关联含不存在的节点或无效关系")
                pair = tuple(sorted((a, b)))
                left, right = sources[a], sources[b]
                if pair in seen or len(related[left["path"]]) >= 3 or len(related[right["path"]]) >= 3:
                    continue
                seen.add(pair)
                for first, second in [(left, right), (right, left)]:
                    related[first["path"]].append(
                        {"path": second["path"], "title": second["title"], "reason": reason}
                    )
            fresh = [e["path"] for group in self.topic_groups().values() for e in group][-80:]
            if should_stop() or fresh != [e["path"] for e in entries]:
                return
            for entry in entries:
                entry["related_notes"] = related[entry["path"]]
            self.ledger["relations"] = {"fingerprint": fingerprint, "done": True, "edges": len(seen)}
        except Exception:
            self.ledger["relations"] = {
                "fingerprint": fingerprint,
                "done": False,
                "retry_at": time.time() + 300,
            }
        self.save()

    def connect_topics(self):
        with self.store.lock:
            self._connect_topics()

    def _connect_topics(self):
        rows = {r["digest"]: r for r in self.store.rows(all_rows=True)}
        for material, entry in self.ledger["notes"].items():
            topic = self.ledger["topics"].get(entry["topic"]) if entry["sufficient"] else None
            row = rows.get(material)
            path = inside(self.vault, entry["path"])
            if not row or row["editing"] or row["draft"] or row["manual_note"] or not path.exists():
                continue
            if self.respect_privacy and row["disposition"] != "active":
                continue
            if file_hash(path) != entry["hash"]:
                continue
            value = json.loads(row["result"])
            desired = topic["path"] if topic else ""
            related = entry.get("related_notes", [])
            if value.get("topic_note", "") == desired and value.get("related_notes", []) == related:
                continue
            value["topic_note"] = desired
            value["related_notes"] = related
            self.store.update(row["id"], result=json.dumps(value, ensure_ascii=False))
            note = _write_note(self.store, self.store.get(row["id"]), self.vault)
            entry["hash"] = file_hash(note)
        self.save()

    def write_index(self):
        rel = "10 Knowledge Hub/Agent导航.md"
        target = inside(self.vault, rel)
        previous = self.ledger.get("index_hash")
        if target.exists() and (not previous or file_hash(target) != previous):
            return  # Keep a user-edited navigation page intact.
        groups = self.topic_groups(include_paused=True)
        available = [e for e in self.ledger["notes"].values() if inside(self.vault, e["path"]).is_file()]
        text = "# Agent 知识导航\n\n> 自动维护的资料入口。原始来源保留，AI 草稿未代表本人核验。\n\n"
        text += f"已整理 {sum(e['sufficient'] for e in available)} 份独立资料；待补充 {sum(not e['sufficient'] for e in available)} 份。\n\n## 专题\n\n"
        for topic, entries in sorted(groups.items()):
            summary = self.ledger["topics"].get(topic)
            text += (
                "- " + (wiki(summary["path"], topic) if summary else topic) + f" · {len(entries)} 份资料\n"
            )
        text += "\n## 分类资料\n"
        for category in CATEGORIES:
            entries = [e for e in available if e["category"] == category and e["sufficient"]]
            if entries:
                text += (
                    "\n### "
                    + category
                    + "\n\n"
                    + "\n".join("- " + wiki(e["path"], e["title"]) for e in entries)
                    + "\n"
                )
        pending = [e for e in available if not e["sufficient"]]
        if pending:
            text += (
                "\n## 待补充原文\n\n" + "\n".join("- " + wiki(e["path"], e["title"]) for e in pending) + "\n"
            )
        payload = text.encode("utf-8")
        if not target.exists() or target.read_bytes() != payload:
            atomic_write(target, payload)
        self.ledger["index_hash"] = file_hash(target)
        self.save()


def semantic_hash(entry):
    value = {
        k: entry.get(k) for k in ("title", "summary", "points", "tags", "category", "topic", "review_notes")
    }
    if entry.get("index_only"):
        value["index_coverage"] = entry.get("coverage", {})
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextlib.contextmanager
def maintenance_lock(root):
    import psutil

    path = Path(root) / "organizer.lock"
    if path.exists():
        try:
            owner = int(path.read_text())
            if psutil.pid_exists(owner):
                yield False
                return
        except (ValueError, OSError):
            pass
        path.unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    try:
        yield True
    finally:
        if path.exists() and path.read_text() == str(os.getpid()):
            path.unlink()


def maintain_once(store, config, cloud=call_agent, should_stop=lambda: False):
    """One incremental material per pass, then affected topics and navigation."""
    if not config.get("organizer_enabled") or not config.get("ssh_enabled"):
        return False
    with maintenance_lock(store.root) as acquired:
        if not acquired:
            return False
        agent = Organizer(store, config["vault"], respect_privacy=True)
        for row in reversed(store.rows()):
            if should_stop():
                return False
            if row["disposition"] != "active" or row["editing"] or row["draft"] or not row["result"]:
                continue
            if row["state"] not in {"done", "source_only", "organizing"} or not row["note_path"]:
                continue
            note = inside(agent.vault, row["note_path"])
            if not note.is_file():
                continue
            result = json.loads(row["result"])
            failure = agent.ledger.get("failures", {}).get(row["digest"], {})
            if failure.get("retry_at", 0) > time.time():
                continue
            prior = agent.ledger["notes"].get(row["digest"])
            current_hash = file_hash(note)
            manual = bool(row["manual_note"] or (row["note_hash"] and row["note_hash"] != current_hash))
            metadata_changed = (
                prior and prior.get("metadata_version", row["metadata_version"]) != row["metadata_version"]
            )
            if prior and not manual and current_hash != prior["hash"]:
                prior["hash"] = current_hash  # A trusted local save already updated the managed note.
                agent.save()
            if result.get("organized") and prior and not metadata_changed:
                if current_hash == prior["hash"]:
                    expected_state = "done" if result["status"] == "processed" else "source_only"
                    if row["state"] != expected_state:
                        store.update_active(row["id"], state=expected_state, stage="")
                    continue
                if not manual:
                    prior["hash"] = current_hash
                    agent.save()
                    agent.connect_topics()
                    continue
                if prior.get("observed_hash") == current_hash:
                    continue
            old_state = "done" if result.get("status") == "processed" else "source_only"
            store.update_active(row["id"], state="organizing", stage="Agent 判断归属与关联", progress=-1)
            try:
                from .core import editor_document

                text = (
                    editor_document(row, agent.vault)["content"]
                    if manual
                    else (row["edited_body"] or result.get("original") or row["body"])
                )
                review_notes = result.get("review_notes") or (prior or {}).get("review_notes", [])
                if (
                    result.get("index_only")
                    and not manual
                    and result.get("metadata_version") == row["metadata_version"]
                    and not (review_notes and not result.get("review_notes"))
                ):
                    value = dict(
                        result
                    )  # The upload already produced validated classification in one model call.
                else:
                    preview = (
                        text[:6000]
                        if manual
                        else json.dumps(
                            {k: result.get(k) for k in ("title", "purpose", "summary", "points")},
                            ensure_ascii=False,
                        )
                    )
                    value = cloud(
                        config,
                        {
                            "mode": "classify",
                            "title": result.get("title", row["name"]),
                            "remarks": row["instructions"]
                            + "\n我的标签："
                            + row["user_tags"]
                            + "\n审阅补充："
                            + "\n".join(review_notes),
                            "text": preview,
                        },
                    )
                    for key in ("index_only", "processing_mode", "coverage", "purpose"):
                        if key in result:
                            value[key] = result[key]
                    if manual and result.get("index_only"):
                        value["coverage"] = {
                            "method": "edited_note_preview",
                            "label": "仅取手改笔记前6000字符重新判断用途，正文保留",
                        }
                validate_analysis(value)
                value["original"] = "" if result.get("index_only") else text
                value["attachments"] = re.findall(
                    r"!?\[\[([^\]|]+\.(?:png|jpg|jpeg|webp|pdf|txt))",
                    note.read_text(encoding="utf-8-sig"),
                    re.I,
                )
                with store.lock:
                    fresh = store.get(row["id"])
                    if (
                        should_stop()
                        or not fresh
                        or fresh["disposition"] != "active"
                        or fresh["editing"]
                        or fresh["draft"]
                        or fresh["metadata_version"] != row["metadata_version"]
                        or file_hash(note) != current_hash
                    ):
                        return False
                    if manual:
                        updated = dict(
                            result, **value, organized=True, metadata_version=fresh["metadata_version"]
                        )
                        updated["status"] = "processed" if value["sufficient"] else "source_only"
                        store.update(row["id"], result=json.dumps(updated, ensure_ascii=False))
                        entry = {
                            "id": row["id"],
                            "path": row["note_path"],
                            "title": value["title"],
                            "summary": value["summary"],
                            "points": value["points"],
                            "tags": value["tags"],
                            "category": value["category"],
                            "topic": value["topic"],
                            "sufficient": value["sufficient"],
                            "sources": (prior or {}).get("sources", []),
                            "hash": (prior or {}).get("hash", ""),
                            "observed_hash": current_hash,
                            "human_modified": True,
                            "metadata_version": fresh["metadata_version"],
                            "index_only": bool(value.get("index_only")),
                            "coverage": value.get("coverage", {}),
                            "purpose": value.get("purpose", ""),
                        }
                        agent.ledger["notes"][row["digest"]] = entry
                        agent.save()
                    else:
                        sources = (prior or {}).get("sources", [])
                        archive_rel = ""
                        if not prior and not result.get("index_only"):
                            archive_rel = (
                                "09 Archive/Agent原始收集/"
                                + safe_name(note.stem, 110)
                                + "-"
                                + current_hash[:8]
                                + ".md"
                            )
                            sources = [archive_rel]
                        agent.apply(fresh, value, sources)
                        if archive_rel and row["note_path"] != agent.ledger["notes"][row["digest"]]["path"]:
                            agent.archive(row["note_path"], current_hash)
                        elif (
                            not prior
                            and result.get("index_only")
                            and row["note_path"] != agent.ledger["notes"][row["digest"]]["path"]
                        ):
                            agent.archive(row["note_path"], current_hash)
                store.update_active(row["id"], state="organizing", stage="Agent 维护专题与导航", progress=-1)
                agent.update_topics(config, cloud=cloud, should_stop=should_stop)
                agent.ledger.setdefault("failures", {}).pop(row["digest"], None)
                agent.save()
                return True
            except Exception as exc:
                agent.ledger.setdefault("failures", {})[row["digest"]] = {
                    "message": str(exc)[:200],
                    "retry_at": time.time() + 300,
                }
                agent.save()
                return False
            finally:
                fresh = store.get(row["id"])
                if fresh and fresh["state"] == "organizing":
                    final_result = json.loads(fresh["result"]) if fresh["result"] else {}
                    final_state = "done" if final_result.get("status") == "processed" else old_state
                    if final_result.get("status") == "source_only":
                        final_state = "source_only"
                    store.update(
                        row["id"], state=final_state, stage="", progress=100 if final_state == "done" else 0
                    )
        if not should_stop():
            agent.update_topics(config, cloud=cloud, should_stop=should_stop)
        return False
