from pathlib import Path

from orbit_kg import OrbitKGClient

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "fixtures" / "pws_km14955.json"


def test_fixture_mode_lists_queries():
    with OrbitKGClient(fixture_path=FIXTURE) as client:
        assert client.mode == "fixture"
        names = list(client.list_fixture_queries())
        assert "phenotypes" in names
        assert "dlk1_annotation" in names


def test_run_named_phenotype_rows():
    with OrbitKGClient(fixture_path=FIXTURE) as client:
        rows = client.run_named("phenotypes")
        assert rows
        assert any("NESTIN" in r["phenotype"] for r in rows)


def test_dlk1_annotation_counts():
    with OrbitKGClient(fixture_path=FIXTURE) as client:
        row = client.run_named("dlk1_annotation")[0]
        assert row["string_partners"] == 50
        assert row["crispick_designs"] == 68
