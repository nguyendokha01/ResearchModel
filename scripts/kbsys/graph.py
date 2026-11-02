"""Local, evidence-derived graph data for the web knowledge map."""

import re

from . import config, concepts, db, discovery, presentation, find


MAX_NODES = 10000
MAX_EDGES = 30000
MAX_DISCOVERY_NODES = 1000


def _species_by_paper(paper_ids):
    found = {}
    for paper_id in paper_ids:
        values = set()
        for unit in db.read_jsonl(db.paper_dir(paper_id) / "evidence.jsonl"):
            species = ((unit.get("model") or {}).get("species") or "").strip()
            if species:
                values.add(species)
        found[paper_id] = values
    return found


def build(con, vault, year_from=None, year_to=None, species=None, relation_type=None):
    # A vault that exists but has no papers yet gets an empty graph, not "no such vault".
    if not con.execute("SELECT 1 FROM paper_vaults WHERE vault = ? LIMIT 1", (vault,)).fetchone() and not (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", vault or "") and config.vault_dir(vault).is_dir()):
        return None
    papers = [dict(r) for r in con.execute(
        "SELECT p.paper_id,p.doi,p.title,p.first_author,p.year,p.evidence_type,p.note_name,p.publication_types "
        "FROM papers p JOIN paper_vaults pv ON pv.paper_id=p.paper_id "
        "WHERE pv.vault=? ORDER BY p.year,p.first_author,p.paper_id", (vault,))]
    ids = {p["paper_id"] for p in papers}
    species_by_paper = _species_by_paper(ids)
    # "Human" and "human" are one filter choice
    every = sorted({s for values in species_by_paper.values() for s in values}, reverse=True)
    all_species = sorted({s.casefold(): s for s in every}.values(), key=str.casefold)
    years = sorted({p["year"] for p in papers if p.get("year")})
    if species:
        papers = [p for p in papers if species.casefold() in {s.casefold() for s in species_by_paper.get(p["paper_id"], set())}]
    if year_from is not None:
        papers = [p for p in papers if p.get("year") is not None and p["year"] >= year_from]
    if year_to is not None:
        papers = [p for p in papers if p.get("year") is not None and p["year"] <= year_to]
    paper_by_id = {p["paper_id"]: p for p in papers}
    paper_ids = set(paper_by_id)

    nodes, edges = {}, {}
    def add_node(node):
        nodes.setdefault(node["id"], node)
    def add_edge(edge):
        edges.setdefault(edge["id"], edge)

    for p in papers:
        p["evidence_type"]=find.effective_evidence_type(p)
        add_node({"id": "paper:" + p["paper_id"], "kind": "paper", "paper_id": p["paper_id"],
                  "label": f"{p.get('first_author') or 'Unknown'} {p.get('year') or 'n.d.'}",
                  "title": p.get("title") or "", "year": p.get("year"),
                  "doi": p.get("doi"), "evidence_type": p.get("evidence_type"), "note_name": p.get("note_name"),
                  "entity_kind": "paper", "roles": [], "color": presentation.COLORS["paper"]})

    concept_path = config.STORE / "concepts.jsonl"
    records = list(db.read_jsonl(concept_path)) if concept_path.exists() else []
    registry = concepts.Registry(records)
    display_metadata = {r["concept_id"]: presentation.metadata_for_term(r["name"], r["type"]) for r in records}
    for pid in sorted(paper_ids):
        for unit in db.read_jsonl(db.paper_dir(pid) / "evidence.jsonl"):
            for record in records:
                metadata = display_metadata[record["concept_id"]]
                for role in presentation.roles_for_unit(record["name"], unit, record.get("synonyms") or []):
                    metadata["role_evidence"].setdefault(role, []).append(unit.get("unit_id"))
    for metadata in display_metadata.values():
        metadata["roles"] = sorted(metadata["role_evidence"])
        metadata["tags"] += ["kb-role/" + role for role in metadata["roles"]]
    links = [dict(r) for r in con.execute(
        "SELECT concept_id,paper_id,level,n_sentences,sections,first_sentence_id FROM concept_links "
        "WHERE paper_id IN ({}) ORDER BY concept_id,paper_id".format(",".join("?" for _ in paper_ids)),
        sorted(paper_ids))] if paper_ids else []
    present_concepts = {r["concept_id"] for r in links}
    for record in records:
        if record["concept_id"] in present_concepts:
            add_node({"id": "concept:" + record["concept_id"], "kind": "concept", "concept_id": record["concept_id"],
                      "label": record["name"], "concept_type": record["type"], "mesh_ui": record.get("mesh_ui"),
                      **display_metadata[record["concept_id"]]})
    for row in links:
        a, b = "paper:" + row["paper_id"], "concept:" + row["concept_id"]
        if a in nodes and b in nodes:
            add_edge({"id": "link:" + row["paper_id"] + ":" + row["concept_id"],
                      "source": a, "target": b, "kind": "paper_concept", "level": row["level"],
                      "label": row["level"], "n_sentences": row["n_sentences"]})

    present_ids = {n["concept_id"] for n in nodes.values() if n["kind"] == "concept"}
    for record in records:
        child = record["concept_id"]
        if child not in present_ids:
            continue
        for parent in record.get("parents") or []:
            parent_record = registry.by_id.get(parent)
            if parent_record:
                parent_id = parent_record["concept_id"]
                add_node({"id": "concept:" + parent_id, "kind": "concept", "concept_id": parent_id,
                          "label": parent_record["name"], "concept_type": parent_record["type"],
                          "mesh_ui": parent_record.get("mesh_ui"),
                          **presentation.metadata_for_term(parent_record["name"], parent_record["type"])})
            elif parent.startswith("mesh:"):
                parent_id = parent
                add_node({"id": "concept:" + parent_id, "kind": "concept", "concept_id": parent_id,
                          "label": "MeSH " + parent[5:], "concept_type": "mesh_parent", "mesh_ui": parent[5:]})
            else:
                continue
            add_edge({"id": "parent:" + parent_id + ":" + child, "source": "concept:" + parent_id,
                      "target": "concept:" + child, "kind": "concept_parent", "label": "parent"})

    if paper_ids:
        refs = con.execute("SELECT ref_id,paper_id,resolved_paper_id FROM references_list "
                           "WHERE paper_id IN ({}) AND resolved_paper_id IS NOT NULL".format(
                               ",".join("?" for _ in paper_ids)), sorted(paper_ids))
        for r in refs:
            if r["resolved_paper_id"] in paper_ids and r["resolved_paper_id"] != r["paper_id"]:
                source, target = "paper:" + r["paper_id"], "paper:" + r["resolved_paper_id"]
                add_edge({"id": "citation:" + r["ref_id"], "source": source, "target": target,
                          "kind": "citation", "label": "cites", "ref_id": r["ref_id"]})

    relation_path = config.STORE / "relations.jsonl"
    relations = list(db.read_jsonl(relation_path)) if relation_path.exists() else []
    unit_concepts = {}
    for paper_id in paper_ids:
        source_text={s['sentence_id']:s.get('text','') for s in db.read_jsonl(db.paper_dir(paper_id)/'sentences.jsonl') if s.get('section') not in ('references','front_matter','abstract')}
        for unit in db.read_jsonl(db.paper_dir(paper_id) / "evidence.jsonl"):
            copied=' '.join(source_text.get(sid,'') for sid in unit.get('sentence_ids',[]))
            ids_for_unit={r['concept_id'] for r in records if presentation.matches_term(copied,r['name'],r.get('synonyms') or [])}
            if ids_for_unit:
                unit_concepts[unit["unit_id"]] = ids_for_unit
    unit_paper = {uid: uid.rsplit(":", 1)[0] for rel in relations for uid in (rel.get("from_unit"), rel.get("to_unit"))
                  if isinstance(uid, str) and ":" in uid}
    from . import relations as relation_store
    for rel in relations:
        rel=dict(rel)
        if rel.get('status') not in relation_store.ACTIVE_RELATION_STATUSES or rel.get('vault') and rel['vault']!=vault:
            continue
        from . import evidence, neural
        for key in ('from_unit','to_unit'):
            uid=rel.get(key)
            if uid:
                resolved=evidence.resolve_unit(con,uid)
                rel[key]=resolved['unit_id'] if resolved else None
        if not all(rel.get(k) and rel[k].rsplit(':',1)[0] in paper_ids for k in ('from_unit','to_unit')):
            continue
        try:
            left_sources=neural.resolve_sources(con,vault,[rel['from_unit']],12)
            right_sources=neural.resolve_sources(con,vault,[rel['to_unit']],12)
        except ValueError:
            # Changed/missing source rows cannot sustain an active graph relation.
            continue
        if not left_sources['items'] or not right_sources['items'] or left_sources['unresolved_ids'] or right_sources['unresolved_ids']:
            continue
        kind = rel.get("type") or "misc"
        if relation_type and kind != relation_type:
            continue
        if kind in ("contradicts", "direction_differs", "effect_vs_no_effect"):
            # Disagreements link the two papers directly, so they stay visible even when both
            # results belong to the same concept.
            pa, pb = rel['from_unit'].rsplit(':',1)[0],rel['to_unit'].rsplit(':',1)[0]
            if pa and pb and pa != pb and "paper:" + pa in nodes and "paper:" + pb in nodes:
                add_edge({"id": "conflict:" + (rel.get("relation_id") or f"{pa}:{pb}"),
                          "source": "paper:" + pa, "target": "paper:" + pb, "kind": "relation", "label": kind,
                          "measure": rel.get("measure_group") or rel.get("outcome_measure"),
                          'source_sides':[left_sources,right_sources],
                          "status": rel.get("status") or "ai_proposed"})
        for left in unit_concepts.get(rel.get("from_unit"), set()):
            for right in unit_concepts.get(rel.get("to_unit"), set()):
                if left == right:
                    continue
                source, target = "concept:" + left, "concept:" + right
                if source in nodes and target in nodes:
                    rid = rel.get("relation_id") or f"{rel.get('from_unit')}:{rel.get('to_unit')}:{kind}"
                    add_edge({"id": "relation:" + rid + ":" + left + ":" + right, "source": source,
                              "target": target, "kind": "relation", "label": kind,
                              'source_sides':[left_sources,right_sources],
                              "status": rel.get("status") or "ai_proposed"})

    # OpenAlex results are bibliographic discovery candidates, never evidence links.
    discovery_truncated = False
    discovery_count = 0
    batches = discovery.iter_cached(vault) if not relation_type or relation_type == "discovered_citation" else ()
    for batch in batches:
        seed_id = "paper:" + batch.get("seed_paper_id", "")
        if seed_id not in nodes:
            continue
        for item in batch.get("candidates") or []:
            cid = item.get("candidate_id")
            if not cid:
                continue
            node_id = "candidate:" + cid
            is_new_candidate = node_id not in nodes
            if is_new_candidate and discovery_count >= MAX_DISCOVERY_NODES:
                discovery_truncated = True
                break
            add_node({"id": node_id, "kind": "candidate", "candidate_id": cid,
                      "label": item.get("title") or cid, "title": item.get("title") or "",
                      "doi": item.get("doi"), "year": item.get("year"),
                      "source": item.get("source"), "cited_by_count": item.get("cited_by_count"),
                      "source_record_url": item.get("source_record_url"),
                      "already_in_library": item.get("already_in_library", False),
                      "landing_page_url": item.get("landing_page_url"),
                      "seed_paper_id": batch.get("seed_paper_id")})
            if not relation_type or relation_type == "discovered_citation":
                add_edge({"id": "discovered:" + cid + ":" + batch["seed_paper_id"],
                          "source": seed_id, "target": node_id, "kind": "discovered_citation",
                          "label": "OpenAlex: cites seed", "source_name": "OpenAlex"})
            if is_new_candidate:
                discovery_count += 1

    all_relation_types = sorted({r.get("type") for r in relations if r.get("type")})
    if any(n["kind"] == "candidate" for n in nodes.values()):
        all_relation_types.append("discovered_citation")
    node_rows = list(nodes.values())
    edge_rows = list(edges.values())
    truncated = discovery_truncated or len(node_rows) > MAX_NODES or len(edge_rows) > MAX_EDGES
    node_rows, edge_rows = node_rows[:MAX_NODES], edge_rows[:MAX_EDGES]
    valid = {n["id"] for n in node_rows}
    edge_rows = [e for e in edge_rows if e["source"] in valid and e["target"] in valid]
    return {"vault": vault, "nodes": node_rows, "edges": edge_rows, "years": years, "species": all_species,
            "relation_types": all_relation_types, "truncated": truncated,
            "counts": {"nodes": len(node_rows), "edges": len(edge_rows), "papers": len(paper_by_id),
                       "concepts": sum(n["kind"] == "concept" for n in node_rows)}}
