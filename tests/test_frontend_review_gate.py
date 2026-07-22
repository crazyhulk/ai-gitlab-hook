import unittest
from types import SimpleNamespace

from app.handlers import _update_frontend_review_status


class FakeGitLabClient:
    def __init__(self, changes, approved_usernames=(), changes_error=None):
        self.changes = changes
        self.approved_usernames = approved_usernames
        self.changes_error = changes_error
        self.statuses = []

    def get_mr_changes(self, project_id, mr_iid):
        if self.changes_error:
            raise self.changes_error
        return self.changes

    def get_mr_approvals(self, project_id, mr_iid):
        return {
            "approved_by": [
                {"user": {"username": username}}
                for username in self.approved_usernames
            ]
        }

    def set_commit_status(self, **kwargs):
        self.statuses.append(kwargs)
        return True


class FrontendReviewGateTest(unittest.TestCase):
    def make_config(self, client):
        return SimpleNamespace(
            gitlab_client=client,
            frontend_review_path="frontend-v1/",
            frontend_required_reviewers=["yangzhengpeng01", "wangqiyuan01"],
        )

    def payload(self):
        return {"project": {"id": 123}}

    def attrs(self):
        return {
            "iid": 45,
            "url": "https://gitlab.example/mr/45",
            "last_commit": {"id": "a" * 40},
        }

    def test_frontend_change_requires_named_reviewer(self):
        client = FakeGitLabClient([{"new_path": "frontend-v1/src/app.ts"}])

        checked = _update_frontend_review_status(
            self.make_config(client), self.payload(), self.attrs()
        )

        self.assertTrue(checked)
        self.assertEqual(client.statuses[-1]["state"], "failed")
        self.assertEqual(client.statuses[-1]["name"], "frontend-review-check")

    def test_either_named_reviewer_can_approve(self):
        client = FakeGitLabClient(
            [{"old_path": "frontend-v1/src/app.ts", "new_path": "web/app.ts"}],
            approved_usernames=["someone", "wangqiyuan01"],
        )

        _update_frontend_review_status(
            self.make_config(client), self.payload(), self.attrs()
        )

        self.assertEqual(client.statuses[-1]["state"], "success")
        self.assertIn("wangqiyuan01", client.statuses[-1]["description"])

    def test_unrelated_change_passes(self):
        client = FakeGitLabClient([{"new_path": "backend/app.py"}])

        _update_frontend_review_status(
            self.make_config(client), self.payload(), self.attrs()
        )

        self.assertEqual(client.statuses[-1]["state"], "success")

    def test_diff_query_failure_blocks_merge(self):
        client = FakeGitLabClient([], changes_error=RuntimeError("GitLab unavailable"))

        _update_frontend_review_status(
            self.make_config(client), self.payload(), self.attrs()
        )

        self.assertEqual(client.statuses[-1]["state"], "failed")
        self.assertIn("检查执行失败", client.statuses[-1]["description"])


if __name__ == "__main__":
    unittest.main()
