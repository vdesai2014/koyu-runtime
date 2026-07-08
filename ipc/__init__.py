"""ipc — the live substrate: shared types + (later) the iceoryx2 port wrappers.

For now this package holds the cross-language struct definitions (``types``) and
the boot-time checks (``checks``) that warden fires as hooks. It imports nothing
from warden; the composition root wires its checks into warden's boot.
"""
