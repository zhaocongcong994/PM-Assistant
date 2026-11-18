import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import teardown_upload
from product_teardown import PHASES, TeardownError, TeardownService

SKILL = Path(__file__).resolve().parents[1] / "skills" / "product-teardown"
SELECTION = {"provider_id": "example-provider", "model": "example-model"}


def _reply(label, level="页面事实", screenshots=None):
    return {"reports": [{"title": label, "sections": [{"title": "贡献 <script>", "items": [{
        "title": "可核对的发现", "text": '页面显示操作已完成。<script>alert(1)</script>',
        "level": level, "screenshots": ["S02"] if screenshots is None else screenshots,
    }]}]}]}


class _Model:
    def __init__(self):
        self.messages = self
        self.calls = []
        self.image_counts = []
        self.responses = {}

    def with_options(self, **kwargs):
        return self

    def create(self, model, system, messages, **kwargs):
        content = messages[0]["content"]
        images = []
        if isinstance(content, str):
            label = content.split("「", 1)[1].split("」", 1)[0]
        else:
            label = content[0]["text"].split("「", 1)[1].split("」", 1)[0]
            images = [block for block in content if block["type"] == "image"]
            if label not in ("用户旅程图", "Agent 契约", "截图观察"):
                assert not images
            assert all(block["source"]["media_type"] == "image/jpeg" for block in images)
        assert "证据" in system and model == SELECTION["model"]
        self.calls.append(label)
        self.image_counts.append(len(images))
        response = self.responses.get(label, _reply(label))
        if callable(response):
            response = response(messages)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(response))])


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    model = _Model()
    service = TeardownService(tmp_path, SKILL, lambda selection: {**selection}, lambda _: model)
    monkeypatch.setattr(service, "_start", lambda *args: None)
    return service, model


@pytest.fixture
def example_request():
    screenshots = [
        {
            "name": f"step-{index:02}.png",
            "data": base64.b64encode(_encoded_image(color=color)).decode("ascii"),
        }
        for index, color in enumerate(("red", "green", "blue"), start=1)
    ]
    return {"title": "测试产品", "model_selection": SELECTION, "screenshots": screenshots}


def test_example_confirmation_retry_and_offline_report(workflow, example_request):
    service, model = workflow
    run_id = service.create(example_request)["id"]
    with pytest.raises(TeardownError, match="用户旅程待确认"):
        service.advance(run_id, "confirm")
    service._work(run_id, SELECTION)
    review = service.read(run_id)
    assert review["status"] == "review"
    assert list(review["results"]) == ["journey"]
    assert [screen["id"] for screen in review["screenshots"]] == ["S01", "S02", "S03"]
    data, content_type = service.screenshot(run_id, "S02")
    assert content_type == "image/png" and data == base64.b64decode(example_request["screenshots"][1]["data"])

    service.advance(run_id, "confirm")
    with pytest.raises(TeardownError) as duplicate:
        service.advance(run_id, "confirm")
    assert duplicate.value.status == 409
    model.responses["产品全景架构图"] = {"reports": []}
    service._work(run_id, SELECTION)
    failed = service.read(run_id)
    assert failed["status"] == "failed" and failed["phase"] == "architecture"
    assert list(failed["results"]) == ["journey", "contracts", "prompts"]
    assert "报告结构不完整" in failed["error"]

    del model.responses["产品全景架构图"]
    service.advance(run_id, "retry")
    service._work(run_id, SELECTION)
    assert service.read(run_id)["status"] == "complete"
    assert model.calls == [phase[0] for phase in PHASES.values()][:4] + ["产品全景架构图", "产品全景架构图", "拆解汇总"]
    for phase, (title, _, _) in PHASES.items():
        preview = service.report_html(run_id, phase)
        assert preview.startswith("<!doctype html>") and f"<h1>{title}</h1>" in preview
        assert f'/api/teardown/runs/{run_id}/screenshots/S02' in preview
        assert "&lt;script&gt;" in preview and "<script>" not in preview
        assert "<input" not in preview
    with zipfile.ZipFile(io.BytesIO(service.archive(run_id))) as bundle:
        assert {"index.html", "journey.html", "agent-contracts.html", "agent-prompts.html",
                "architecture.html", "图片清单.csv", "screenshots/S02.png"}.issubset(bundle.namelist())
        journey = bundle.read("journey.html").decode()
        assert 'href="screenshots/S02.png"' in journey
        assert "&lt;script&gt;" in journey and "<script>" not in journey
        assert bundle.testzip() is None


def test_interrupted_run_recovers_without_losing_review(workflow, example_request):
    service, _ = workflow
    run_id = service.create(example_request)["id"]
    service._work(run_id, SELECTION)
    reviewed = service.read(run_id)["results"]["journey"]
    service.advance(run_id, "confirm")
    recovered = TeardownService(service.state_dir, SKILL, lambda x: x, lambda _: None)
    recovered.recover()
    current = recovered.read(run_id)
    assert current["status"] == "failed" and current["phase"] == "contracts"
    assert current["results"]["journey"] == reviewed
    assert "重启" in current["error"]
    assert recovered.summaries()[0]["status"] == "failed"


def test_invalid_model_output_gets_one_repair_attempt(workflow, example_request):
    service, model = workflow
    run_id = service.create(example_request)["id"]

    def answer(messages):
        if len(messages) == 1:
            return _reply("用户旅程图", screenshots=[])
        assert messages[-2]["role"] == "assistant"
        assert "S01, S02, S03" in messages[-1]["content"]
        return _reply("用户旅程图")

    model.responses["用户旅程图"] = answer
    service._work(run_id, SELECTION)
    assert model.calls == ["用户旅程图", "用户旅程图"]
    assert service.read(run_id)["status"] == "review"


def test_prompt_phase_preview_includes_every_agent(workflow, example_request):
    service, model = workflow
    run_id = service.create(example_request)["id"]
    service._work(run_id, SELECTION)
    model.responses["参考 System Prompt"] = {
        "reports": [*_reply("剧情 Agent")["reports"], *_reply("剪辑 Agent")["reports"]],
    }
    service.advance(run_id, "confirm")
    service._work(run_id, SELECTION)
    preview = service.report_html(run_id, "prompts")
    assert "<h1>参考 System Prompt</h1>" in preview
    assert "<h2>剧情 Agent</h2>" in preview and "<h2>剪辑 Agent</h2>" in preview
    assert 'id="report-1-section-1"' in preview and 'id="report-2-section-1"' in preview


def test_downstream_findings_with_broken_references_are_marked_unconfirmed(workflow, example_request):
    service, model = workflow
    run_id = service.create(example_request)["id"]
    service._work(run_id, SELECTION)
    model.responses["参考 System Prompt"] = _reply("参考 Prompt", screenshots=["S02", "S99"])
    service.advance(run_id, "confirm")
    service._work(run_id, SELECTION)
    run = service.read(run_id)
    assert run["status"] == "complete"
    finding = run["results"]["prompts"][0]["sections"][0]["items"][0]
    assert finding["level"] == "尚未确认"
    assert finding["screenshots"] == ["S02"]


def test_invalid_and_duplicate_images_are_rejected_without_a_partial_run(workflow, example_request):
    service, _ = workflow
    first = example_request["screenshots"][0]
    damaged = {"name": "broken.png", "data": base64.b64encode(b"broken").decode()}
    with pytest.raises(TeardownError, match="已损坏"):
        service.create({**example_request, "screenshots": [first, damaged]})
    with pytest.raises(TeardownError, match="重复截图"):
        service.create({**example_request, "screenshots": [first, {**first, "name": "copy.png"}]})
    assert not list(service.state_dir.iterdir())


def test_jpeg_is_served_with_the_verified_type(workflow, example_request):
    service, _ = workflow
    encoded = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(encoded, "JPEG")
    image = {"name": "example.jpeg", "data": base64.b64encode(encoded.getvalue()).decode()}
    run_id = service.create({**example_request, "screenshots": [image]})["id"]
    assert service.screenshot(run_id, "S01") == (encoded.getvalue(), "image/jpeg")


def _encoded_image(format_name="PNG", color="red"):
    output = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format_name)
    return output.getvalue()


@pytest.mark.parametrize(("format_name", "extension"), [
    ("WEBP", "webp"), ("GIF", "gif"), ("BMP", "bmp"), ("TIFF", "tiff"),
    ("AVIF", "avif"), ("HEIF", "heic"), ("JPEG2000", "jp2"),
])
def test_common_image_formats_are_convertible(workflow, example_request, tmp_path, format_name, extension):
    service, _ = workflow
    path = tmp_path / f"test.{extension}"
    path.write_bytes(_encoded_image(format_name))
    run = service.create(example_request, uploads=[("screenshots", path.name, path)])
    data, mime = service.screenshot(run["id"], "S01")
    assert mime == "image/png"
    with Image.open(io.BytesIO(data)) as preview:
        preview.load()
        assert preview.size == (24, 24)


def test_fifty_images_are_allowed_and_batch_observations_are_reused(workflow, example_request, tmp_path):
    service, model = workflow
    uploads = []
    for index in range(50):
        path = tmp_path / f"step-{index}.png"
        path.write_bytes(_encoded_image(color=(index, index, index)))
        uploads.append(("screenshots", path.name, path))
    with pytest.raises(TeardownError, match="50 张"):
        service.create(example_request, uploads=[*uploads, uploads[0]])
    run = service.create(example_request, uploads=uploads)
    assert len(run["screenshots"]) == 50 and run["screenshots"][-1]["id"] == "S50"

    service._work(run["id"], SELECTION)
    assert service.read(run["id"])["status"] == "review"
    assert model.image_counts == [10] * 5 + [0]
    assert "visual_batches" not in service.public_run(service.read(run["id"]))

    service.advance(run["id"], "confirm")
    service._work(run["id"], SELECTION)
    assert service.read(run["id"])["status"] == "complete"
    assert sum(model.image_counts) == 50


def test_zip_sorts_members_and_retains_original(workflow, example_request, tmp_path):
    service, _ = workflow
    path = tmp_path / "样例.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("folder/screen-10.png", _encoded_image(color="blue"))
        archive.writestr("folder/screen-2.webp", _encoded_image("WEBP"))
        archive.writestr("__MACOSX/._screen-1.png", b"placeholder")
        archive.writestr("README.txt", "notes")
    source = ("archive", path.name, path)
    with pytest.raises(TeardownError, match="一个 ZIP"):
        service.create(example_request, uploads=[source, ("screenshots", "a.png", path)])
    run = service.create(example_request, uploads=[source])
    assert [item["name"] for item in run["screenshots"]] == ["folder/screen-2.webp", "folder/screen-10.png"]
    assert run["upload_archive"] == {"name": "样例.zip", "size": path.stat().st_size}
    stored, name = service.source_archive(run["id"])
    assert name == path.name and stored.read_bytes() == path.read_bytes()

    service._work(run["id"], SELECTION)
    with zipfile.ZipFile(io.BytesIO(service.archive(run["id"]))) as report:
        assert report.read("source.zip") == path.read_bytes()
        journey = report.read("journey.html").decode()
        assert 'href="source.zip"' in journey and 'href="screenshots/S02.png"' in journey


@pytest.mark.parametrize("failure", ["compressed", "expanded", "path", "invalid"])
def test_zip_rejects_invalid_archives_and_limits(workflow, example_request, tmp_path, monkeypatch, failure):
    service, _ = workflow
    source = tmp_path / "test.zip"
    if failure == "invalid":
        source.write_bytes(b"not an archive")
        message = "无法解压"
    else:
        with zipfile.ZipFile(source, "w") as archive:
            filename = "../test.png" if failure == "path" else "test.png"
            archive.writestr(filename, _encoded_image())
        message = "无效的文件路径"
        if failure == "compressed":
            monkeypatch.setattr(teardown_upload, "MAX_ZIP_BYTES", source.stat().st_size - 1)
            message = "ZIP 压缩包不能超过"
        elif failure == "expanded":
            monkeypatch.setattr(teardown_upload, "MAX_ZIP_EXPANDED_BYTES", 60)
            message = "解压后不能超过"
    with pytest.raises(TeardownError, match=message):
        service.create(example_request, uploads=[("archive", source.name, source)])


def test_multipart_rejects_mixed_files_and_oversized_parts(tmp_path, monkeypatch):
    content_type = "multipart/form-data; boundary=nexus"
    metadata = b'--nexus\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n{}\r\n'
    image = b'--nexus\r\nContent-Disposition: form-data; name="screenshots"; filename="s.png"\r\n\r\nhello\r\n'
    archive = b'--nexus\r\nContent-Disposition: form-data; name="archive"; filename="z.zip"\r\n\r\nworld\r\n'
    ending = b"--nexus--\r\n"

    def parse(parts):
        body = metadata + parts + ending
        return teardown_upload.parse_upload(io.BytesIO(body), content_type, len(body), tmp_path)

    assert parse(image)[1][0][2].read_bytes() == b"hello"
    with pytest.raises(TeardownError, match="单独上传图片或上传一个 ZIP"):
        parse(image + archive)
    monkeypatch.setattr(teardown_upload, "MAX_ZIP_BYTES", 4)
    with pytest.raises(TeardownError, match="ZIP 压缩包不能超过") as oversized:
        parse(archive)
    assert oversized.value.status == 413


def test_project_import_reuses_retry_and_copies_complete_evidence(workflow, example_request, tmp_path, monkeypatch):
    import shutil
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    import web_app

    teardowns, _ = workflow
    records = tmp_path / "workspace"
    monkeypatch.setattr(web_app, "teardowns", teardowns)
    monkeypatch.setattr(web_app, "STATE_DIR", records)
    monkeypatch.setattr(web_app, "STATE_PATH", records / "sessions.json")
    monkeypatch.setattr(web_app, "OUTPUT_DIR", records / "generated")
    monkeypatch.setattr(web_app, "_resolve_model", lambda selection: selection)

    run_id = teardowns.create(example_request)["id"]
    request_id = str(uuid.uuid4())
    with pytest.raises(TeardownError) as incomplete:
        web_app._create_teardown_project(run_id, request_id)
    assert incomplete.value.status == 409

    teardowns._work(run_id, SELECTION)
    teardowns.advance(run_id, "confirm")
    teardowns._work(run_id, SELECTION)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [job.result() for job in (
            pool.submit(web_app._create_teardown_project, run_id, request_id),
            pool.submit(web_app._create_teardown_project, run_id, request_id),
        )]
    assert sorted([first[0], second[0]]) == [200, 201]
    project = first[1]
    assert project["id"] == second[1]["id"] and len(web_app._read_state()["sessions"]) == 1
    assert project["title"] == f"{example_request['title']} · 产品拆解" and project["phase"] == "brief"
    assert not project["messages"] and not project["discovery"]["started"]
    assert "## 关键发现" in project["brief"] and "## 待确认问题" in project["brief"]
    assert "产品拆解参考资料" in web_app._discovery_context(project)
    assert "用户导入了产品拆解参考资料" in web_app._discovery_context(project)

    materials = project["teardown_import"]
    output = web_app.OUTPUT_DIR / project["id"] / "teardown"
    shutil.rmtree(teardowns.state_dir / run_id)
    assert {report["path"] for report in materials["reports"]} == {
        "index.html", "journey.html", "agent-contracts.html", "agent-prompts.html", "architecture.html",
    }
    assert all((output / filename).is_file() for filename in materials["files"])
    assert 'href="screenshots/S02.png"' in (output / "journey.html").read_text()
    tool = SimpleNamespace(type="tool_use", name="save_files", input={
        "files": [{"path": "teardown/index.html", "content": "generated"}],
    })
    with pytest.raises(ValueError, match="保存着导入的拆解资料"):
        web_app._validate_generated_files(project["id"], SimpleNamespace(content=[tool]))
    assert web_app._create_teardown_project(run_id, request_id)[0] == 200


def test_failed_project_import_leaves_no_session_or_files(workflow, example_request, tmp_path, monkeypatch):
    import uuid

    import web_app

    teardowns, _ = workflow
    monkeypatch.setattr(web_app, "teardowns", teardowns)
    monkeypatch.setattr(web_app, "STATE_PATH", tmp_path / "sessions.json")
    monkeypatch.setattr(web_app, "OUTPUT_DIR", tmp_path / "generated")
    monkeypatch.setattr(web_app, "_resolve_model", lambda selection: selection)
    run_id = teardowns.create(example_request)["id"]
    teardowns._work(run_id, SELECTION)
    teardowns.advance(run_id, "confirm")
    teardowns._work(run_id, SELECTION)

    def fail_save(_):
        raise OSError("storage failure")

    monkeypatch.setattr(web_app, "_save_state", fail_save)
    with pytest.raises(TeardownError, match="无法保存拆解资料"):
        web_app._create_teardown_project(run_id, str(uuid.uuid4()))
    assert web_app._read_state()["sessions"] == []
    assert list(web_app.OUTPUT_DIR.iterdir()) == []
