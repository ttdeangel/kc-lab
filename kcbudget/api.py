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
    def __init__(self, url, read_only=False, timeout=60):
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

    def create(self, body):
        body = {k: v for k, v in body.items() if k != "id"}
        r = _data(self._call("POST", "/model/budget", body=body)) or {}
        return r.get("id") if isinstance(r, dict) else None

    def update(self, bid, body):
        body = dict(body, id=bid)
        if self.detect() == "rest":
            return _data(self._call("PUT", f"/model/budget/{bid}", body=body))
        return _data(self._call("POST", "/model/budget", body=body))

    def delete(self, bid):
        if self.detect() == "rest":
            return self._call("DELETE", f"/model/budget/{bid}")
        try:
            return self._call("DELETE", "/model/deleteBudget", {"id": bid})
        except KubecostError as e:
            if "HTTP 404" in str(e) or "HTTP 405" in str(e):
                return self._call("DELETE", f"/model/budget/{bid}")
            raise

    # ---------- spend (Allocation API)
    def spend(self, filt, start, end):
        """Total allocated cost for a Kubecost filter between two RFC3339 times."""
        d = _data(self.get("/model/allocation", window=f"{start},{end}", aggregate="cluster",
                           accumulate="true", idle="false", filter=filt)) or [{}]
        sets = d if isinstance(d, list) else [d]
        return round(sum(float((a or {}).get("totalCost") or 0) for s in sets for a in (s or {}).values()), 4)
