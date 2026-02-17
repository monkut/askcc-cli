import pytest

from askcc.functions import _parse_issue_url


class TestParseIssueUrl:
    def test_valid_issue_url(self):
        owner, repo, issue_number = _parse_issue_url("https://github.com/monkut/askcc-cli/issues/42")
        assert owner == "monkut"
        assert repo == "askcc-cli"
        assert issue_number == 42

    def test_invalid_url_missing_issues_segment(self):
        with pytest.raises(ValueError):
            _parse_issue_url("https://github.com/monkut/askcc-cli/pull/1")

    def test_invalid_url_too_few_parts(self):
        with pytest.raises(ValueError):
            _parse_issue_url("https://github.com/monkut")

    def test_valid_url_with_trailing_slash(self):
        owner, repo, issue_number = _parse_issue_url("https://github.com/monkut/askcc-cli/issues/7/")
        assert owner == "monkut"
        assert repo == "askcc-cli"
        assert issue_number == 7
