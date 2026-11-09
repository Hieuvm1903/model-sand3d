# warehouse-models

3D assets for the digital-twin app, written in [sand3d](https://github.com/Sandflow-ai/sand3d).
One file per model in `models/`. On every push to `main`, GitHub Actions builds them all and publishes
them to Supabase Storage under `warehouse/<hash>/`, then registers the version in the `model_sets` table.

## Add a model
Create `models/<name>.py` with `build()` returning a sand3d root. That's it: it is built as `<name>.glb`
and appears in `layout.json`'s asset list. (Use `build_json(assets)` for non-mesh data.)

## Local
    python3.13 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python tools/build.py                 # -> dist/
    python tools/build.py --only robot    # one model

## One-time setup
1. Run `supabase/schema.sql` in the Supabase SQL editor.
2. GitHub repo -> Settings -> Secrets and variables -> Actions: add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
   (service_role key, Project Settings -> API). Never use this key in the browser.
