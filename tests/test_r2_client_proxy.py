"""Tests for joyverse/config.py's _R2ClientProxy.

Regression: the proxy forwarded only get_object and put_object. list_topics
calls list_objects_v2, so topic discovery raised AttributeError against real
R2 while passing every test -- FakeR2 always had the method, so the fake never
exposed the gap.
"""
import pytest

from conftest import FakeR2


class TestProxyForwardsEveryCall:
    @pytest.fixture
    def proxy(self, monkeypatch):
        from joyverse import config as jv_config

        fake = FakeR2()
        monkeypatch.setattr(jv_config, "r2_client", fake, raising=False)
        return jv_config.r2_client, fake

    def test_get_object(self, proxy):
        client, fake = proxy
        fake.seed("users/u_a1b2c3d4e5f6/profile.md", "x")
        assert client.get_object(
            Bucket="b", Key="users/u_a1b2c3d4e5f6/profile.md")

    def test_put_object(self, proxy):
        client, fake = proxy
        client.put_object(Bucket="b", Key="k", Body=b"v")
        assert fake.store["k"] == b"v"

    def test_list_objects_v2(self, proxy):
        client, fake = proxy
        fake.seed("users/u_a1b2c3d4e5f6/data/dsa/progress.json", "{}")
        out = client.list_objects_v2(
            Bucket="b", Prefix="users/u_a1b2c3d4e5f6/data/")
        assert [o["Key"] for o in out["Contents"]] == [
            "users/u_a1b2c3d4e5f6/data/dsa/progress.json"]

    def test_delete_object(self, proxy):
        client, fake = proxy
        fake.seed("k", "v")
        client.delete_object(Bucket="b", Key="k")
        assert "k" not in fake.store

    def test_delete_objects(self, proxy):
        client, fake = proxy
        fake.seed("a", "1")
        fake.seed("b", "2")
        client.delete_objects(Bucket="b",
                              Delete={"Objects": [{"Key": "a"}, {"Key": "b"}]})
        assert fake.store == {}


class TestProxyMatchesWhatJoyverseCalls:
    """The proxy must cover every S3 method the app actually uses."""

    def test_every_used_method_is_forwarded(self):
        import pathlib
        import re

        source = "\n".join(
            p.read_text() for p in pathlib.Path("joyverse").glob("*.py"))
        used = set(re.findall(r"r2_client\.(\w+)\(", source))

        from joyverse import config as jv_config

        missing = {m for m in used if not hasattr(jv_config.r2_client, m)}
        assert not missing, (
            "r2_client proxy is missing methods joyverse calls: "
            f"{sorted(missing)}")
