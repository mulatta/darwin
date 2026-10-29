---
name: record
description: Commit a research record (for example an experiment design) together with the provenance of the clean notebook run that produced it, list and inspect records, and reproduce a record by re-running its exact notebook. Use when a design or result is final and must be traceable to its inputs, code, and environment.
---

# Record

    result = await nb.verify(["layout", "volumes"])       # pair skill
    rec = record.commit(
        "experiment_design",
        {"title": "qPCR of target genes", "layout": result.outputs["layout"].hash, "reps": 3},
        result,
        rationale="Approved layout after power check.",
    )
    record.show(rec.id)                 # spec hash, notebook hash, input and output hashes, env, author, session
    rec.load_spec()                     # the committed spec as a dict
    record.list(kind="experiment_design")
    await record.reproduce(rec.id)      # {} when every output hash matches

Rules:

- Only commit a result returned by `await nb.verify(...)`. Values seen in the kernel or in scratch output are not records.
- `spec` is a JSON object describing the result for humans and downstream tools; refer to large outputs by their hash (`result.outputs[name].hash`) instead of copying them.
- Records are append-only. To change a design, fix the cell, verify again, and commit a new record.
- `reproduce` re-runs the stored notebook file with its pinned inputs and the recorded interpreter in a fresh process; a non-empty result lists outputs whose hashes changed.
