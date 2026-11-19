from fastapi.testclient import TestClient

from app.cli import main
from app.config import PROJECT_ROOT, Settings
from app.web.server import create_app


def test_demo_import_and_ask(tmp_path, monkeypatch):
    db_path = str(tmp_path / "demo.db")
    data_dir = str(PROJECT_ROOT / "demo")
    monkeypatch.setenv("SCHOOL_DB_PATH", db_path)
    monkeypatch.setenv("SCHOOL_DATA_DIR", data_dir)
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    settings = Settings(db_path=db_path, data_dir=data_dir, embedding_enabled=False, deploy_mode="public")

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/ready").status_code == 503
        assert main(["import", "--profile", "demo"]) == 0
        assert client.get("/api/v1/ready").json() == {"status": "ready"}

        response = client.post("/api/v1/ask", json={"question": "纸质图书的借阅期限是多久？"})
        assert response.status_code == 200
        answer = response.json()
        assert answer["found"] and "三十天" in answer["answer"]
        assert {citation["doc_id"] for citation in answer["citations"]} == {"demo:library-2025"}

        missing = client.post("/api/v1/ask", json={"question": "校园巴士几点发车？"})
        assert missing.status_code == 200 and not missing.json()["found"]
