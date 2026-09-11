"""Migration preserves accepted TeX bytes and never mixes sidecars into exports."""

import pytest


def fixture_paths(tmp_path):
    source = tmp_path / "Downloads/U1/batches/A31/sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source-pdf")
    pending = source.parent.parent / "A32/pending.pdf"
    pending.parent.mkdir()
    pending.write_bytes(b"pending-pdf")
    old = tmp_path / "data/U1/batches/A31/optimized/sample.optimized.tex"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"original TeX contents\n")
    sidecar = old.with_name("sample.report.json")
    sidecar.write_bytes(b'{"existing":true}')
    return source, old, sidecar


def test_migration_mirrors_source_tree_and_preserves_tex_and_sidecars(tmp_path):
    from .publication import migrate_exports

    source, old, sidecar = fixture_paths(tmp_path)
    result = migrate_exports(tmp_path / "Downloads", tmp_path / "data", tmp_path / "data/optimized")
    new = tmp_path / "data/optimized/U1/batches/A31/sample.tex"
    assert new.read_bytes() == b"original TeX contents\n"
    assert not old.exists() and not sidecar.exists()
    assert (tmp_path / "data/U1/batches/A31/.pipeline/sample/published-sidecars/sample.report.json").read_bytes() == b'{"existing":true}'
    assert list(new.parent.iterdir()) == [new]
    assert (tmp_path / "data/optimized/U1/batches/A32").is_dir()
    assert not (tmp_path / "data/optimized/U1/batches/A32/pending.tex").exists()
    assert source.read_bytes() == b"source-pdf"
    assert result["tex_moved"] == 1 and result["sidecars_moved"] == 1
    assert migrate_exports(tmp_path / "Downloads", tmp_path / "data", tmp_path / "data/optimized")["tex_moved"] == 0


def test_migration_preflights_conflicts_before_moving_any_file(tmp_path):
    from .publication import migrate_exports

    _, old, sidecar = fixture_paths(tmp_path)
    new = tmp_path / "data/optimized/U1/batches/A31/sample.tex"
    new.parent.mkdir(parents=True)
    new.write_text("different existing result")
    with pytest.raises(FileExistsError):
        migrate_exports(tmp_path / "Downloads", tmp_path / "data", tmp_path / "data/optimized")
    assert old.exists() and sidecar.exists()
    assert new.read_text() == "different existing result"
