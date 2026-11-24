"""GitHub repos: cloud.github, repo= on create and run, push, pull_request and the GitHub errors."""

import asyncio
import json
import unittest
import urllib.parse

import kosa8
from kosa8 import Kosa8Cloud
from kosa8.retry import RetryPolicy
from kosa8.aio import AsyncCloud

KEY = "k8c_live_testkey"


class Fake:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        self.seen.append({"method": method, "path": u.path, "query": u.query,
                          "body": json.loads(body) if body else None})
        st, payload = self.answer(self.seen[-1])
        return st, json.dumps(payload).encode(), {}

    async def aopen(self, *a):
        return self(*a)


def running(**extra):
    return {"id": "sb_abc", "state": "running", "template": "base", **extra}


REPO = {"full_name": "acme/api", "ref": "main", "sha": "abc", "path": "/workspace/api", "status": "cloned"}


def answer(s):
    p = s["path"]
    if p == "/v1/github":
        return 200, {"configured": True, "connected": True, "can_manage": False, "install_url": None, "installations": []}
    if p == "/v1/github/repos":
        return 200, {"repos": [{"full_name": "acme/api", "private": True, "default_branch": "main",
                                "html_url": "https://github.com/acme/api", "installation_id": 7}], "next_page": 2}
    if p.endswith("/branches"):
        return 200, {"default_branch": "main", "branches": [{"name": "main", "sha": "abc", "protected": True}]}
    if p == "/v1/run":
        return 200, {"run": {"id": "sr_1", "status": "ok", "exit": 0, "repo": REPO}}
    if p.endswith("/repo"):
        return 200, {"repo": {**REPO, "changes": {"files_changed": 2}}}
    if p.endswith("/github/push"):
        return 200, {"branch": "kosa8/abc", "sha": "def", "compare_url": "https://github.com/acme/api/compare/x",
                     "files_changed": 2}
    if p.endswith("/github/pull-request"):
        return 200, {"number": 12, "url": "https://github.com/acme/api/pull/12", "branch": "fix", "sha": "def"}
    return 200, {"sandbox": running(repo=REPO)}


class GitHub(unittest.TestCase):
    def test_status_repos_branches(self):
        fake = Fake(answer)
        cloud = Kosa8Cloud(KEY, env={}, opener=fake)
        self.assertTrue(cloud.github.status()["connected"])
        self.assertEqual(cloud.github.repos("api")[0]["full_name"], "acme/api")
        self.assertEqual(cloud.github.repos_page("x", page=2)["next_page"], 2)
        self.assertTrue(cloud.github.branches("acme/api")["branches"][0]["protected"])
        self.assertEqual([(s["method"], s["path"], s["query"]) for s in fake.seen], [
            ("GET", "/v1/github", ""),
            ("GET", "/v1/github/repos", "q=api"),
            ("GET", "/v1/github/repos", "q=x&page=2"),
            ("GET", "/v1/github/repos/acme/api/branches", ""),
        ])

    def test_bad_repo_refused_before_any_request(self):
        fake = Fake(answer)
        cloud = Kosa8Cloud(KEY, env={}, opener=fake)
        with self.assertRaisesRegex(ValueError, "owner/name"):
            cloud.github.branches("acme")
        with self.assertRaisesRegex(ValueError, "owner/name"):
            cloud.sandboxes.create(repo="not a repo")
        with self.assertRaisesRegex(ValueError, "empty ref"):
            cloud.sandboxes.create(repo="acme/api@")
        with self.assertRaisesRegex(ValueError, "depth"):
            cloud.run(command="ls", repo={"name": "acme/api", "depth": 0})
        self.assertEqual(fake.seen, [])

    def test_repo_on_create_and_run(self):
        fake = Fake(answer)
        cloud = Kosa8Cloud(KEY, env={}, opener=fake)
        box = cloud.sandboxes.create(repo="acme/api@main")
        self.assertEqual(box.info["repo"]["status"], "cloned")
        self.assertEqual(fake.seen[0]["body"]["repo"], "acme/api@main")
        cloud.sandboxes.create(repo={"name": "acme/api", "ref": "v1.2", "path": "/workspace/x", "depth": 5})
        self.assertEqual(fake.seen[1]["body"]["repo"], {"name": "acme/api", "ref": "v1.2", "path": "/workspace/x", "depth": 5})
        r = cloud.run(command="npm test", repo="acme/api")
        self.assertEqual(r["repo"]["full_name"], "acme/api")
        self.assertEqual(fake.seen[2]["body"], {"command": "npm test", "repo": "acme/api"})

    def test_repo_push_pull_request(self):
        fake = Fake(answer)
        cloud = Kosa8Cloud(KEY, env={}, opener=fake)
        box = cloud.sandboxes.get("sb_abc")
        self.assertEqual(box.repo()["changes"]["files_changed"], 2)
        self.assertEqual(box.info["repo"]["full_name"], "acme/api")
        self.assertEqual(box.push()["branch"], "kosa8/abc")
        box.push(branch="fix", message="m")
        self.assertEqual(box.pull_request(title="Fix", base="main", draft=False)["number"], 12)
        self.assertEqual([(s["method"], s["path"], s["body"]) for s in fake.seen[1:]], [
            ("GET", "/v1/sandboxes/sb_abc/repo", None),
            ("POST", "/v1/sandboxes/sb_abc/github/push", {}),
            ("POST", "/v1/sandboxes/sb_abc/github/push", {"branch": "fix", "message": "m"}),
            ("POST", "/v1/sandboxes/sb_abc/github/pull-request", {"title": "Fix", "base": "main", "draft": False}),
        ])

    def test_async(self):
        fake = Fake(answer)

        async def go():
            async with AsyncCloud(KEY, env={}, async_opener=fake.aopen) as cloud:
                self.assertEqual((await cloud.github.repos())[0]["full_name"], "acme/api")
                self.assertEqual((await cloud.github.branches("acme/api"))["default_branch"], "main")
                box = await cloud.sandboxes.create(repo="acme/api")
                self.assertEqual((await box.repo())["full_name"], "acme/api")
                self.assertEqual((await box.push(branch="b"))["sha"], "def")
                self.assertEqual((await box.pull_request(title="t"))["number"], 12)
                self.assertEqual((await cloud.run(command="ls", repo="acme/api"))["repo"]["status"], "cloned")

        asyncio.run(go())
        self.assertEqual([s["path"] for s in fake.seen], [
            "/v1/github/repos", "/v1/github/repos/acme/api/branches", "/v1/sandboxes", "/v1/sandboxes/sb_abc/repo",
            "/v1/sandboxes/sb_abc/github/push", "/v1/sandboxes/sb_abc/github/pull-request", "/v1/run",
        ])


class Errors(unittest.TestCase):
    def pr_error(self, status, body):
        fake = Fake(lambda s: (200, {"sandbox": running()}) if s["method"] == "GET" else (status, body))
        cloud = Kosa8Cloud(KEY, env={}, opener=fake, retry=RetryPolicy(attempts=1))
        box = cloud.sandboxes.get("sb_abc")
        with self.assertRaises(kosa8.Kosa8ApiError) as cm:
            box.pull_request()
        return cm.exception

    def test_not_connected(self):
        e = self.pr_error(409, {"error": "github_not_connected", "message": "Connect GitHub first.",
                                "connect_url": "https://app.kosa8.com/app#settings/github"})
        self.assertIsInstance(e, kosa8.Kosa8GitHubNotConnectedError)
        self.assertIsInstance(e, kosa8.Kosa8ConflictError)
        self.assertEqual(e.connect_url, "https://app.kosa8.com/app#settings/github")

    def test_permissions(self):
        e = self.pr_error(409, {"error": "github_permissions", "message": "Approve.", "missing": ["pull_requests:write"],
                                "settings_url": "https://github.com/settings/installations/7"})
        self.assertIsInstance(e, kosa8.Kosa8GitHubPermissionsError)
        self.assertEqual(e.missing_permissions, ["pull_requests:write"])
        self.assertEqual(e.settings_url, "https://github.com/settings/installations/7")

    def test_other_codes(self):
        self.assertIsInstance(self.pr_error(409, {"error": "no_changes", "message": "Nothing changed."}),
                              kosa8.Kosa8NoChangesError)
        nf = self.pr_error(404, {"error": "repo_not_found", "message": "No such repo."})
        self.assertIsInstance(nf, kosa8.Kosa8RepoNotFoundError)
        self.assertIsInstance(nf, kosa8.Kosa8NotFoundError)
        un = self.pr_error(502, {"error": "github_unavailable", "message": "GitHub is busy.", "retry_after_s": 30})
        self.assertIsInstance(un, kosa8.Kosa8GitHubUnavailableError)
        self.assertEqual(un.retry_after, 30)
        plain = self.pr_error(409, {"error": "not_running", "message": "Not running."})
        self.assertIs(type(plain), kosa8.Kosa8ConflictError)


if __name__ == "__main__":
    unittest.main()
