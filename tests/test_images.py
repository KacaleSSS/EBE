"""Offline tests: every cloud/download boundary is mocked."""
import base64
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, PngImagePlugin

from ebe import images
from ebe.export import export_book


KEY = "transient-secret-123456789"
CONFIG = {"vision_model": "independent-vision-model"}
MANIFEST = [{"slug": "diagram", "prompt": "A green forest without text", "alt": "Forest"}]


def png():
    stream = io.BytesIO()
    meta = PngImagePlugin.PngInfo()
    meta.add_text("private", "must be removed")
    Image.new("RGB", (32, 24), "green").save(stream, "PNG", pnginfo=meta)
    return stream.getvalue()


def generation(data=None):
    return {"data": [{"b64_json": base64.b64encode(png() if data is None else data).decode()}]}


def vision(value=None):
    return {"choices": [{"message": {"content": json.dumps(value if value is not None else {
        "pass": True, "text_present": False, "relevant": True, "reason": "Looks correct"})}}]}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(images.network, "request_json", side_effect=AssertionError("unexpected API call")) as api:
        with patch.object(images.network, "_request", side_effect=AssertionError("unexpected download")) as download:
            yield api, download


def run(config=None, manifest=None):
    return images.generate_images(MANIFEST if manifest is None else manifest, "assets", CONFIG if config is None else config, KEY, enabled=True)


def successful(offline, manifest=None):
    offline[0].side_effect = [generation(), vision()]
    report = run(manifest=manifest)
    assert report["status"] == "complete"
    return report


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_disabled_has_no_side_effects(offline, enabled):
    assert images.generate_images(None, "assets", None, None, enabled)["status"] == "disabled"
    offline[0].assert_not_called()
    assert not Path("assets").exists()


@pytest.mark.parametrize("config", [{}, {"vision_model": "doubao-seedream-3-0-t2i-250415"},
    {"vision_model": "SEEDREAM-other"}, {"vision_model": "same", "generation_model": "same"},
    {**CONFIG, "base_url": "http://example.com"}, {**CONFIG, "base_url": "https://user:pass@example.com"},
    {**CONFIG, "base_url": "https://example.com?key=x"}, {**CONFIG, "timeout": True},
    {**CONFIG, "api_key": KEY}, {**CONFIG, "allow_image_hosts": ["*.example.com"]}])
def test_invalid_config_before_payment(offline, config):
    with pytest.raises(ValueError):
        run(config)
    offline[0].assert_not_called()


@pytest.mark.parametrize("manifest", [[], MANIFEST * 11, MANIFEST * 2, {"images": MANIFEST},
    [{**MANIFEST[0], "slug": "../escape"}], [{**MANIFEST[0], "slug": "con"}],
    [{**MANIFEST[0], "prompt": "a" * 2001}], [{**MANIFEST[0], "alt": "a" * 201}],
    [{**MANIFEST[0], "book": "never upload a book"}], [{**MANIFEST[0], "prompt": KEY}]])
def test_invalid_manifest_before_payment(offline, manifest):
    with pytest.raises(ValueError):
        run(manifest=manifest)
    offline[0].assert_not_called()


def test_success_receipt_metadata_and_payload(offline):
    successful(offline)
    raw = Path("assets/diagram.receipt.json").read_text()
    assert KEY not in raw and MANIFEST[0]["prompt"] not in raw and "Looks correct" not in raw
    receipt = json.loads(raw)
    assert receipt["prompt_sha256"] == images._hash(MANIFEST[0]["prompt"].encode())
    with Image.open("assets/diagram.png") as result:
        assert result.size == (32, 24)
        assert not result.info
    calls = offline[0].call_args_list
    assert calls[0].args[0] == images.DEFAULT_BASE_URL + "/images/generations"
    assert calls[0].args[1]["model"] == images.DEFAULT_MODEL
    assert calls[0].args[2] == KEY
    payload = calls[1].args[1]
    assert payload["model"] == CONFIG["vision_model"]
    url = payload["messages"][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",")[1]) == Path("assets/diagram.png").read_bytes()
    assert images.insert_images("Before\r\n[[IMAGE:diagram]]\r\nAfter", "assets") == "Before\r\n![Forest](diagram.png)\r\nAfter"


@pytest.mark.parametrize("value", [
    {"pass": False, "text_present": False, "relevant": True, "reason": "no"},
    {"pass": True, "text_present": True, "relevant": True, "reason": "text"},
    {"pass": True, "text_present": False, "relevant": False, "reason": "wrong"},
    {"pass": "true", "text_present": False, "relevant": True, "reason": "wrong"},
    {"pass": True, "text_present": False, "relevant": True, "reason": "x" * 501},
    {"pass": True}, {"pass": True, "text_present": False, "relevant": True, "reason": "", "extra": 1}])
def test_vision_fail_closed(offline, value):
    offline[0].side_effect = [generation(), vision(value)]
    assert run()["status"] == "failed"
    assert not list(Path("assets").iterdir())
    with pytest.raises(ValueError):
        images.insert_images("[[IMAGE:diagram]]", "assets")


@pytest.mark.parametrize("content", ['```json\n{}\n```', '{"pass":true,"pass":false}', '[]', 'NaN'])
def test_non_strict_vision(offline, content):
    response = vision()
    response["choices"][0]["message"]["content"] = content
    offline[0].side_effect = [generation(), response]
    assert run()["status"] == "failed"


@pytest.mark.parametrize("failure", [RuntimeError(KEY), KeyboardInterrupt()])
def test_stop_no_retry_or_secret_leak(offline, failure):
    offline[0].side_effect = failure
    report = run(manifest=MANIFEST + [{**MANIFEST[0], "slug": "second"}])
    assert report["status"] in {"failed", "aborted"}
    assert report["images"][1]["status"] == "not_attempted"
    assert KEY not in json.dumps(report)
    assert offline[0].call_count == 1


def test_bad_image_does_not_reach_vision(offline):
    offline[0].side_effect = [generation(b"<svg onload='alert(1)'></svg>")]
    assert run()["images"][0]["reason"] == "image_decode_failed"
    assert offline[0].call_count == 1


def test_dimension_limit(offline, monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 100)
    offline[0].side_effect = [generation()]
    assert run()["status"] == "failed"
    assert offline[0].call_count == 1


def test_base64_preferred_and_no_fallback(offline):
    offline[0].side_effect = [{"data": [{"b64_json": "invalid!", "url": "https://images.example.com/a"}]}]
    assert run({**CONFIG, "allow_image_hosts": ["images.example.com"]})["status"] == "failed"
    offline[1].assert_not_called()


def test_allowlisted_download_without_auth_or_redirects(offline):
    url = "https://images.example.com/a.png"
    offline[0].side_effect = [{"data": [{"url": url}]}, vision()]
    offline[1].side_effect = None
    offline[1].return_value = (png(), "image/png", url)
    assert run({**CONFIG, "base_url": "https://api.example.com/v1", "allow_image_hosts": ["images.example.com"]})["status"] == "complete"
    assert offline[1].call_args.kwargs == {"max_bytes": images.MAX_BYTES, "timeout": 60, "redirects": 0}
    assert offline[0].call_args_list[0].args[0] == "https://api.example.com/v1/images/generations"


@pytest.mark.parametrize("url", ["http://images.example.com/a", "https://evil.example.com/a",
    "https://images.example.com.evil.com/a", "https://user:secret@images.example.com/a"])
def test_download_policy_before_fetch(offline, url):
    offline[0].side_effect = [{"data": [{"url": url}]}]
    assert run({**CONFIG, "allow_image_hosts": ["images.example.com"]})["status"] == "failed"
    offline[1].assert_not_called()


def test_download_redirect_is_not_retried(offline):
    offline[0].side_effect = [{"data": [{"url": "https://images.example.com/a"}]}]
    offline[1].side_effect = images.network.NetworkError("redirect_refused")
    assert run({**CONFIG, "allow_image_hosts": ["images.example.com"]})["status"] == "failed"
    assert offline[1].call_count == 1


@pytest.mark.parametrize("marker", ["[[IMAGE:../diagram]]", "[[IMAGE:diagram]]\n[[IMAGE:diagram]]",
    "<img src='[[IMAGE:diagram]]'>", "[[IMAGE:missing]]"])
def test_invalid_insertion(offline, marker):
    successful(offline)
    with pytest.raises(ValueError):
        images.insert_images(marker, "assets")


def test_tampered_image_and_receipt(offline):
    successful(offline)
    path = Path("assets/diagram.png")
    original = path.read_bytes()
    path.write_bytes(original + b"tampering")
    with pytest.raises(ValueError, match="image_tampered"):
        images.insert_images("[[IMAGE:diagram]]", "assets")
    path.write_bytes(original)
    receipt_path = Path("assets/diagram.receipt.json")
    receipt = json.loads(receipt_path.read_text())
    receipt["file"] = "../other.png"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        images.insert_images("[[IMAGE:diagram]]", "assets")


def test_no_receipt_no_insertion(offline):
    successful(offline)
    Path("assets/diagram.receipt.json").unlink()
    with pytest.raises(ValueError):
        images.insert_images("[[IMAGE:diagram]]", "assets")


def test_existing_slug_prevents_spend(offline):
    successful(offline)
    offline[0].reset_mock()
    with pytest.raises(ValueError, match="already_exists"):
        run()
    offline[0].assert_not_called()


def test_escape_paths(offline):
    successful(offline)
    for path in ("assets/../assets", "../assets"):
        with pytest.raises(ValueError):
            images.insert_images("[[IMAGE:diagram]]", path)


def test_symlink_refused(offline):
    successful(offline)
    try:
        Path("linked").symlink_to(Path("assets").resolve(), target_is_directory=True)
    except OSError:
        pytest.skip("OS does not grant symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        images.insert_images("[[IMAGE:diagram]]", "linked")
    with pytest.raises(ValueError, match="symlink"):
        images.generate_images([{**MANIFEST[0], "slug": "other"}], "linked", CONFIG, KEY, True)


def test_alt_injection_escaped(offline):
    successful(offline, [{**MANIFEST[0], "alt": '\"](<script>alert(1)</script>) ![evil](https://bad)'}])
    result = images.insert_images("[[IMAGE:diagram]]", "assets")
    assert "<script>" not in result and "&lt;script&gt;" in result
    assert "\\]\\(" in result


def test_plain_markdown_unchanged(offline):
    assert images.insert_images("No images\n", "missing") == "No images\n"


def test_symlink_guard_without_os_privileges(offline):
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(ValueError, match="symlink"):
            run()
    offline[0].assert_not_called()


def test_generation_path_escape_before_payment(offline):
    with pytest.raises(ValueError, match="path_escape"):
        images.generate_images(MANIFEST, "assets/../elsewhere", CONFIG, KEY, True)
    offline[0].assert_not_called()


@pytest.mark.parametrize("key", [None, "", "key with spaces", "key\nheader", "中文"])
def test_transient_key_required(offline, key):
    with pytest.raises(ValueError):
        images.generate_images(MANIFEST, "assets", CONFIG, key, True)
    offline[0].assert_not_called()


def test_failure_preserves_previous_success(offline):
    offline[0].side_effect = [generation(), vision(), TimeoutError(KEY)]
    report = run(manifest=MANIFEST + [{**MANIFEST[0], "slug": "second"}, {**MANIFEST[0], "slug": "third"}])
    assert [row["status"] for row in report["images"]] == ["verified", "failed", "not_attempted"]
    assert offline[0].call_count == 3
    assert images.insert_images("[[IMAGE:diagram]]", "assets") == "![Forest](diagram.png)"


def test_receipt_invalid_validation_even_with_matching_checksum(offline):
    successful(offline)
    path = Path("assets/diagram.receipt.json")
    receipt = json.loads(path.read_text())
    receipt.pop("receipt_sha256")
    receipt["validation"]["pass"] = False
    receipt["receipt_sha256"] = images._hash(images._canonical(receipt))
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="unverified_image"):
        images.insert_images("[[IMAGE:diagram]]", "assets")


def test_asset_directory_not_in_reference(offline):
    successful(offline)
    Path("assets").rename("assets () [tag]#")
    result = images.insert_images("[[IMAGE:diagram]]", "assets () [tag]#")
    assert result == "![Forest](diagram.png)"


@pytest.mark.parametrize("extension", ["html", "epub"])
@pytest.mark.parametrize("path_mode", ["relative", "absolute", "outside_cwd"])
def test_insert_then_real_export(offline, tmp_path, monkeypatch, extension, path_mode):
    successful(offline)
    # Match the CLI's nested exchange directory, including filesystem punctuation.
    assets = tmp_path / "runtime" / "image exchange () [tag]#" / "assets"
    assets.parent.mkdir(parents=True)
    Path("assets").rename(assets)
    original = (assets / "diagram.png").read_bytes()
    if path_mode == "relative":
        assets_arg = assets.relative_to(tmp_path)
    else:
        assets_arg = assets
    if path_mode == "outside_cwd":
        working = tmp_path / "manuscripts"
        working.mkdir()
        monkeypatch.chdir(working)
    markdown = images.insert_images("# Book\n\n[[IMAGE:diagram]]\n", assets_arg)
    assert markdown == "# Book\n\n![Forest](diagram.png)\n"
    output = export_book(markdown, tmp_path / ("book." + extension), assets_dir=assets_arg)
    if extension == "html":
        document = output.read_text(encoding="utf-8")
        assert '<img src="data:image/png;base64,' + base64.b64encode(original).decode() in document
    else:
        with zipfile.ZipFile(output) as archive:
            members = [name for name in archive.namelist() if name.startswith("OEBPS/assets/")]
            assert len(members) == 1
            assert archive.read(members[0]) == original
            document = archive.read("OEBPS/book.xhtml").decode("utf-8")
            assert 'src="' + members[0].removeprefix("OEBPS/") + '"' in document
    # Only the mocked generation/vision calls occurred; export does no networking.
    assert offline[0].call_count == 2
    offline[1].assert_not_called()


def test_example_manifest_offline(offline):
    example = Path(__file__).resolve().parents[1] / "examples" / "image-prompts.json"
    manifest = json.loads(example.read_text(encoding="utf-8"))
    offline[0].side_effect = [response for _ in manifest for response in (generation(), vision())]
    assert run(manifest=manifest)["status"] == "complete"
    assert offline[0].call_count == 2 * len(manifest)
    offline[1].assert_not_called()
