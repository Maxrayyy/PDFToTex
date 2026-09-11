"""Optional JSON enrichment; never rewrites or recompiles the source TEX."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .fields import basic_fields
from .llm import FieldSpec, TableNameRequest, LLMBatchNamer
from .textio import write_utf8_atomic


def tex_hash(tex):
    return hashlib.sha256(tex.encode("utf-8")).hexdigest()


def write_plan(path, tex, records, requests):
    fields = basic_fields(records, tex)
    known = {fid for item in requests for fid in item["field_ids"].values()}
    by_page = {}
    for item in fields:
        if item["field_id"] not in known and item.get("extraction_status") != "invalid":
            by_page.setdefault(item["page"], []).append(item)
    requests = list(requests)
    for page, items in by_page.items():
        specs = [FieldSpec(f"f{i:04d}", "", "", f["value"], f.get("label", ""))
                 for i, f in enumerate(items)]
        req = TableNameRequest(page=page, fields=specs)
        requests.append({"request": asdict(req), "field_ids": {
            spec.key: item["field_id"] for spec, item in zip(specs, items)}})
    payload = {"schema": "semantic-naming-plan/v1", "source_tex_sha256": tex_hash(tex),
               "requests": requests}
    write_utf8_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def stamp_registry(path, tex, *, evidence_path=None, provenance_path=None):
    path = Path(path)
    data = json.loads(path.read_text("utf-8"))
    data["source_tex_sha256"] = tex_hash(tex)
    data["source_registry"] = str(provenance_path) if provenance_path else None
    data["recognition_evidence"] = str(evidence_path) if evidence_path else None
    evidence_fields = {}
    if evidence_path:
        try:
            evidence = json.loads(Path(evidence_path).read_text("utf-8"))
            for page in evidence.get("pages", []):
                for item in page.get("fields", []):
                    evidence_fields.setdefault(item["field_id"], []).append((page, item))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            evidence_fields = {}
            data["evidence_link_error"] = type(exc).__name__
    for item in data["fields"]:
        item.setdefault("name_status", "pending")
        candidates = evidence_fields.get(item["field_id"], [])
        if len(candidates) == 1:
            page, original = candidates[0]
            if item.get("page") == page["page"]:
                item["source_bbox"] = original.get("bbox")
                item["source_render"] = page.get("render")
    write_utf8_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def enrich(args):
    tex_path, registry_path = Path(args.tex), Path(args.registry)
    tex = tex_path.read_text("utf-8")
    digest = tex_hash(tex)
    plan_path = Path(args.plan) if args.plan else tex_path.with_suffix(".naming.json")
    if Path(args.output).resolve() in {tex_path.resolve(), plan_path.resolve()}:
        raise ValueError("Naming output must not overwrite the source TEX or naming plan")
    plan = json.loads(plan_path.read_text("utf-8"))
    data = json.loads(registry_path.read_text("utf-8"))
    if (plan.get("schema") != "semantic-naming-plan/v1" or plan.get("source_tex_sha256") != digest
            or data.get("source_tex_sha256") != digest):
        raise ValueError("Naming plan and registry must match the current TEX; regenerate the plan after edits")
    fields = {item["field_id"]: item for item in data["fields"]}
    if len(fields) != len(data["fields"]):
        raise ValueError("Duplicate field IDs in registry")
    namer = LLMBatchNamer(Path(args.name_cache), model=args.model)
    for item in plan["requests"]:
        if not set(item["field_ids"].values()).issubset(fields):
            continue
        if any(fields[fid].get("extraction_status") == "invalid" for fid in item["field_ids"].values()):
            continue
        raw = dict(item["request"])
        raw["fields"] = [FieldSpec(**f) for f in raw["fields"]]
        request = TableNameRequest(**raw)
        naming = namer.name_table(request)
        if naming.source not in {"llm", "cache"}:
            continue
        for key, fid in item["field_ids"].items():
            fields[fid].update(semantic=naming.fields[key], semantic_alias=f"{fid}-{naming.fields[key]}",
                               table_alias=naming.table, name_source=naming.source, name_status="complete")
    pending = sum(f.get("name_status") != "complete" for f in data["fields"])
    data["semantic_naming"] = {"status": "pending" if pending else "complete", "pending_fields": pending,
                                "model": namer.model, "stats": namer.stats}
    if tex_hash(tex_path.read_text("utf-8")) != digest:
        raise ValueError("TEX changed while semantic naming was in progress")
    write_utf8_atomic(args.output, json.dumps(data, ensure_ascii=False, indent=2))
    return data["semantic_naming"]
