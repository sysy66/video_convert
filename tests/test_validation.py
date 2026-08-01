from unittest.mock import MagicMock, patch

from src.validation import validate_config


def _mock_disk_usage(_path):
    return MagicMock(free=100 * 1024**3)


class TestValidateConfigVmaf:
    def test_vmaf_required_missing_binary_errors(self):
        with (
            patch("src.validation.config.VMAF_REQUIRED", True),
            patch("src.validation.config.VMAF_FFMPEG_PATH", "/nonexistent/ffmpeg"),
            patch("src.validation.config.VMAF_MODEL_PATH", None),
            patch("src.validation.os.path.isfile", return_value=False),
            patch("src.validation.os.access", return_value=True),
            patch("src.validation.config.LIBRARIES", {}),
            patch("src.validation.shutil.disk_usage", side_effect=_mock_disk_usage),
        ):
            errors, warnings = validate_config()

        assert any("VMAF_REQUIRED is True but VMAF_FFMPEG_PATH not found" in e for e in errors)

    def test_vmaf_required_binary_not_executable_errors(self):
        with (
            patch("src.validation.config.VMAF_REQUIRED", True),
            patch("src.validation.config.VMAF_FFMPEG_PATH", "/ffmpeg"),
            patch("src.validation.config.VMAF_MODEL_PATH", None),
            patch("src.validation.os.path.isfile", return_value=True),
            patch("src.validation.os.access", return_value=False),
            patch("src.validation.config.LIBRARIES", {}),
            patch("src.validation.shutil.disk_usage", side_effect=_mock_disk_usage),
        ):
            errors, warnings = validate_config()

        assert any("VMAF_FFMPEG_PATH not executable" in e for e in errors)

    def test_vmaf_required_model_missing_errors(self):
        with (
            patch("src.validation.config.VMAF_REQUIRED", True),
            patch("src.validation.config.VMAF_FFMPEG_PATH", "/ffmpeg"),
            patch("src.validation.config.VMAF_MODEL_PATH", "/model.json"),
            patch("src.validation.os.path.isfile", side_effect=lambda p: p != "/model.json"),
            patch("src.validation.os.access", return_value=True),
            patch("src.validation.config.LIBRARIES", {}),
            patch("src.validation.shutil.disk_usage", side_effect=_mock_disk_usage),
        ):
            errors, warnings = validate_config()

        assert any("VMAF_MODEL_PATH not found" in e for e in errors)

    def test_vmaf_not_required_skips_checks(self):
        with (
            patch("src.validation.config.VMAF_REQUIRED", False),
            patch("src.validation.config.LIBRARIES", {}),
            patch("src.validation.shutil.disk_usage", side_effect=_mock_disk_usage),
        ):
            errors, warnings = validate_config()

        assert not any("VMAF" in e for e in errors)
        assert not any("VMAF" in w for w in warnings)


class TestValidateConfigLibraries:
    def test_missing_input_error(self):
        with (
            patch(
                "src.validation.config.LIBRARIES",
                {
                    "lib1": {
                        "input": "/nonexistent/input",
                        "output": "/tmp/out",
                        "cache": "/tmp/cache",
                    }
                },
            ),
            patch("src.validation.os.path.exists", return_value=False),
            patch("src.validation.config.VMAF_REQUIRED", False),
            patch("src.validation.shutil.disk_usage", side_effect=_mock_disk_usage),
        ):
            errors, warnings = validate_config()

        assert any("input does not exist" in e for e in errors)

    def test_cache_missing_warns_not_errors(self):
        with (
            patch(
                "src.validation.config.LIBRARIES",
                {"lib1": {"input": "/tmp/in", "output": "/tmp/out", "cache": "/nonexistent/cache"}},
            ),
            patch("src.validation.os.path.exists", return_value=False),
            patch("src.validation.config.VMAF_REQUIRED", False),
            patch("src.validation.shutil.disk_usage", side_effect=_mock_disk_usage),
        ):
            errors, warnings = validate_config()

        assert not any("cache" in e for e in errors)
        assert any("cache does not exist" in w for w in warnings)
