from gateforge.target import NetworkTypeIdentifier, SignalTypeIdentifier


LBP_PROVIDER = "lbp"
LBP_TYPE_VERSION = 2
LBP_LOGIC = SignalTypeIdentifier(LBP_PROVIDER, "logic", LBP_TYPE_VERSION)
LBP_WIRE = NetworkTypeIdentifier(LBP_PROVIDER, "wire", LBP_TYPE_VERSION)