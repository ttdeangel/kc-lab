"""kcresize - approval-gated Kubecost container request right-sizing.

Works against any Kubecost 3.x deployment using only its HTTP API:
recommendations (requestSizingV2), cost evidence (Allocation API) and the
change itself (Kubecost Actions). No cluster credentials are required.
"""

__version__ = "1.0.0"
