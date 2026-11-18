"""Kubecost Budget API client.

Documented API (Kubecost 2.x docs, "Governance APIs > Budget API"):
  GET    /model/budgets                  list recurring budget rules
  POST   /model/budget                   create; include "id" in the body to update
  DELETE /model/deleteBudget?id=<id>     delete
Some builds expose REST-style paths instead (/model/budget, /model/budget/{id}). `detect()` probes
both and the client falls back automatically; run `python -m kcbudget doctor` against a new Kubecost
version before the first apply. Transport, retries and auth (KUBECOST_AUTH_HEADER /
KUBECOST_BASIC_USER) are shared with kcresize.
"""
import os

from kcresize.kubecost import Kubecost, KubecostError

__all__ = ["BudgetAPI", "KubecostError"]


def _data(r):
    """Kubecost wraps payloads as {"code":200,"data":...}; some builds return bare lists/objects."""
    if isinstance(r, dict) and "data" in r:
        return r["data"]
    if isinstance(r, dict) and "budgets" in r:
        return r["budgets"]
    return r


class BudgetAPI(Kubecost):
    def __init__(self, url, read_only=False, timeout=None):
        timeout = timeout or float(os.environ.get("KCBUDGET_TIMEOUT", "20"))
        super().__init__(url, read_only=read_only, timeout=timeout)
        # "documented" | "rest" | "" (auto)
        self.style = os.environ.get("KCBUDGET_API_STYLE", "").strip().lower()

    # ---------- discovery
    def detect(self):
        if self.style:
            return self.style
        try:
            self.get("/model/budgets")
            self.style = "documented"
        except KubecostError as e:
            if "HTTP 404" not in str(e):
                raise
            self.get("/model/budget")          # raises if neither exists
            self.style = "rest"
        return self.style

    # ---------- CRUD
    def list(self):
        path = "/model/budgets" if self.detect() == "documented" else "/model/budget"
        d = _data(self.get(path)) or []
        return [b for b in d if isinstance(b, dict)]

    def _find(self, name):
        return [b for b in self.list() if b.get("name") == name]

    def create(self, body):
        """POST once. A create is not idempotent: if the call times out or fails, check whether Kubecost
        made the budget anyway before trying again, so a slow Kubecost never ends up with duplicates."""
        body = {k: v for k, v in body.items() if k != "id"}
        for attempt in range(2):
            try:
                r = _data(self._call("POST", "/model/budget", body=body, attempts=1)) or {}
                bid = r.get("id") if isinstance(r, dict) else None
                return bid or ((self._find(body["name"]) or [{}])[0].get("id"))
            except KubecostError as e:
                if "HTTP 4" in str(e) and "duplicate name" not in str(e):
                    raise
                # timeout / 5xx, or Kubecost 3.3.1 answering "duplicate name" for the budget it just stored:
                # the plan only creates names that were not in Kubecost, so a budget with this name is ours
                found = self._find(body["name"])
                if found:
                    return found[0].get("id")
                if attempt == 1:
                    raise

    def update(self, bid, body):
        body = dict(body, id=bid)
        if self.detect() == "rest":
            return _data(self._call("PUT", f"/model/budget/{bid}", body=body))
        try:
            return _data(self._call("POST", "/model/budget", body=body))
        except KubecostError as e:
            # same 3.3.1 quirk as create: the update may be stored and still answered "duplicate name"
            if "duplicate name" in str(e) and self._matches(bid, body):
                return None
            raise

    def _matches(self, bid, body):
        from . import spec
        cur = [b for b in self.list() if b.get("id") == bid]
        return bool(cur) and spec.normalize(cur[0]) == spec.normalize(body)

    # delete paths in the order tried; the first that works is remembered
    DELETE_PATHS = (("DELETE", "/model/budget", "query"),          # Kubecost 3.3.1: /model/budget?id=
                    ("DELETE", "/model/deleteBudget", "query"),    # Kubecost 2.x docs
                    ("DELETE", "/model/budget/{id}", "path"))      # REST-style builds

    def delete(self, bid):
        paths = self.DELETE_PATHS
        if self.detect() == "rest":
            paths = (paths[2],) + paths[:2]
        if getattr(self, "_delete_path", None):
            paths = (self._delete_path,) + tuple(p for p in paths if p != self._delete_path)
        errors = []
        for method, path, style in paths:
            try:
                if style == "path":
                    r = self._call(method, path.replace("{id}", bid), attempts=1)
                else:
                    r = self._call(method, path, {"id": bid}, attempts=1)
            except KubecostError as e:
                errors.append(str(e))
                if any(f"HTTP {c}" in str(e) for c in (400, 404, 405)):
                    continue
                raise
            if any(b.get("id") == bid for b in self.list()):       # answered OK but still there: try the next path
                errors.append(f"{method} {path}: answered OK but the budget is still listed")
                continue
            self._delete_path = (method, path, style)
            return r
        raise KubecostError("delete failed on every known path: " + " | ".join(errors))

    # ---------- spend (Allocation API)
    def spend(self, filt, start, end):
        """Total allocated cost for a Kubecost filter between two RFC3339 times."""
        d = _data(self.get("/model/allocation", window=f"{start},{end}", aggregate="cluster",
                           accumulate="true", idle="false", filter=filt)) or [{}]
        sets = d if isinstance(d, list) else [d]
        return round(sum(float((a or {}).get("totalCost") or 0) for s in sets for a in (s or {}).values()), 4)
