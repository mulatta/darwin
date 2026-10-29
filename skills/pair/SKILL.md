---
name: pair
description: Work directly inside a live marimo notebook kernel. Explore with throwaway scratch code, keep results as named notebook cells that are saved to the .py file immediately, pin input artifacts by hash, and verify the notebook with a clean run in a fresh process. Use for research computations whose results (experiment design numbers, analysis tables, plate layouts) must be reproducible and readable by humans, before committing them with the record skill.
---

# Pair

All research data lives in the notebook kernel, not in the agent's own Python. The notebook file is written as you work; there is no separate clean-up step.

    nb = await pair.open("analysis/qpcr.py")                 # starts or reattaches; new file uses darwin's env
    nb = await pair.open("analysis/qpcr.py", venv=".venv")    # new file pinned to a project env (uv, conda, pixi)

    await nb.scratch("plate.dtypes")                          # explore: runs in the kernel, never saved
    await nb.input("plate", artifact.get("plate_tsv"))        # pins the artifact by hash in the `inputs` cell

Write each cell as a plain function; its body becomes the cell, so code needs no quoting:

    def clean(plate):                     # name = cell name; parameters = names it reads
        plate_ok = plate[plate.gene.notna()]
        plate_ok.head()                   # last expression is the cell's output
        return plate_ok                   # optional final return = names it exports (dropped)

    await nb.cell(clean)                  # saved + run; dependents rerun; same name replaces the cell

    def dilute(c_mM, target_mM, final_ul=50):
        return final_ul * target_mM / c_mM

    dilute = await nb.define(dilute)      # whole function as its own cell; other cells call it

    await nb.delete("clean")                                  # cell and its variables are gone
    await nb.status("clean")                                  # Cell(status, errors, output)
    await nb.cell("notes", "mo.md('...')")                    # string form only for non-function cells
    result = await nb.verify(["layout", "volumes"])           # fresh process; must equal the live values

Rules:

- `scratch` is for looking: printing, `head()`, shapes, quick checks. Names it defines are gone on the next call, but it can read every cell's variables. Anything a conclusion depends on goes into a `cell`.
- Each variable is defined by one cell only (marimo rule). Replace a cell with the same name instead of redefining its variables elsewhere; use `_name` for cell-local temporaries.
- Never mutate a variable from another cell or from `scratch`; marimo does not track mutation, and `verify` raises `ReproductionError` when live values differ from the clean run.
- Read data through `nb.input(...)` so the notebook pins exact artifact versions. No network calls, LLM calls, or `rlm.spawn` inside cells: store such results as artifacts first and add them as inputs.
- To hand data to a sub-agent, store it from a cell (`artifact.put(...)`) and pass the artifact name or hash.
- Define classes inside a cell function (`define` takes functions only), and never call a cell or defined function here: run it by writing a cell that uses it.
- A failing cell is still saved; the `PairError` says how to fix or delete it.
- Heavy work (alignment, pipelines, long simulations) does not belong in a cell: run it as a job and add its output files as artifacts.
- The file is a plain marimo notebook: a human can open it with `marimo edit <path>` or export it with `marimo export html <path>`.
- Lint notebooks with `marimo check`; `ruff format` works as is. ruff check misreads marimo cells (unused cell variables, output expressions, generated `return`), so a project that runs it ignores those rules for its notebook paths:

      [tool.ruff.lint.per-file-ignores]
      "notebooks/*.py" = ["F841", "B018", "PLR1711"]
