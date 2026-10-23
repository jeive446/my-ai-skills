"""Register existing validated 30fps source shots for a new edit revision."""
import argparse
from pathlib import Path

import generate_wan22_mv as batch
from mv import bind, read


def adopt(manifest, sources):
    path = bind(manifest)
    path, normalized, digest = batch.load_manifest(path)
    mapping_path = Path(sources).resolve()
    mapping = read(mapping_path)
    folder, state_path = batch.project_paths(normalized["project"])
    with batch.project_lock(folder):
        if state_path.exists():
            raise FileExistsError(f"Existing state preserved: {state_path}. Use a new revision project.")
        state = batch.state_for_manifest(normalized, path, digest)
        for shot in normalized["shots"]:
            source = (mapping_path.parent / mapping[shot["id"]]).resolve()
            if not batch.output_is_valid(source, shot["duration"], normalized["width"], normalized["height"]):
                raise ValueError(f"Source does not match planned original 30fps clip: {shot['id']}: {source}")
            state["shots"][shot["id"]].update(status="completed", output=str(source),
                                             provenance="adopted_existing_validated_source", updated_at=batch.now())
        batch.atomic_json_write(state_path, state)
    print(f"Registered {len(state['shots'])} existing clips without GPU submission: {state_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True)
    p.add_argument("--sources", required=True)
    a = p.parse_args()
    adopt(a.manifest, a.sources)
