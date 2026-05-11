"""Hand-curated ticker → sector mapping.

Why hand-curated rather than fetched from yfinance:
- yfinance "sector" field is generic ("Technology", "Healthcare") and misses the
  thematic groupings users actually care about — "AI Chips", "Space Exploration",
  "Quantum Computing", etc.
- yfinance is rate-limited and adds latency.
- For Phase 1/2 the buzz set rarely exceeds ~50 tickers, so curation is cheap.

Each ticker maps to ONE primary theme. We can extend to multi-theme later if needed.
"""

# Order of sectors here is the order they'll appear in the report.
SECTOR_ORDER = [
    "AI Chips & Compute",
    "AI Memory & Storage",
    "AI Software & Models",
    "AI Datacenter & Power",
    "Space Exploration",
    "Quantum Computing",
    "Crypto & Mining",
    "Electric Vehicles",
    "Autonomous & Robotics",
    "Biotech & Pharma",
    "Fintech",
    "Cybersecurity",
    "Cloud & SaaS",
    "Semiconductors (Other)",
    "Mega Cap Tech",
    "Consumer & Retail",
    "Energy",
    "Defense & Aerospace",
    "Banks & Financials",
    "ETFs & Indexes",
    "Meme & Retail-Favorite",
    "Other",
]

# Sectors that always appear in the dashboard, even when no tickers are buzzing
# in them today. These are the user's primary thematic interests — pinning them
# means "this is a section I always want to see, even if it's empty right now."
ALWAYS_SHOW_SECTORS = {
    "AI Chips & Compute",
    "AI Memory & Storage",
    "AI Software & Models",
    "AI Datacenter & Power",
    "Space Exploration",
    "Quantum Computing",
}

TICKER_TO_SECTOR: dict[str, str] = {
    # AI Chips & Compute
    "NVDA": "AI Chips & Compute",
    "AMD": "AI Chips & Compute",
    "AVGO": "AI Chips & Compute",
    "TSM": "AI Chips & Compute",
    "ARM": "AI Chips & Compute",
    "MRVL": "AI Chips & Compute",
    "SMCI": "AI Chips & Compute",  # AI servers
    "DELL": "AI Chips & Compute",
    "NVDL": "AI Chips & Compute",  # 2x NVDA ETF
    "SOXL": "AI Chips & Compute",
    "SOXX": "AI Chips & Compute",
    "SMH": "AI Chips & Compute",

    # AI Memory & Storage
    "MU": "AI Memory & Storage",
    "WDC": "AI Memory & Storage",
    "STX": "AI Memory & Storage",
    "HIMX": "AI Memory & Storage",  # display drivers but heavily AI-correlated lately

    # AI Software & Models
    "PLTR": "AI Software & Models",
    "AI": "AI Software & Models",
    "SOUN": "AI Software & Models",
    "META": "AI Software & Models",  # Llama / FAIR
    "GOOG": "AI Software & Models",
    "GOOGL": "AI Software & Models",
    "MSFT": "AI Software & Models",  # OpenAI partner
    "ORCL": "AI Software & Models",  # AI infra plays
    "DUOL": "AI Software & Models",
    "PATH": "AI Software & Models",  # UiPath

    # AI Datacenter & Power
    "VRT": "AI Datacenter & Power",  # Vertiv
    "ETN": "AI Datacenter & Power",  # Eaton
    "GEV": "AI Datacenter & Power",  # GE Vernova
    "CEG": "AI Datacenter & Power",  # Constellation Energy
    "VST": "AI Datacenter & Power",  # Vistra
    "TLN": "AI Datacenter & Power",  # Talen Energy
    "FLNC": "AI Datacenter & Power",  # Fluence (battery storage for datacenters)
    "AMPX": "AI Datacenter & Power",  # Amprius (high-density batteries)

    # Space Exploration
    "RKLB": "Space Exploration",   # Rocket Lab
    "ASTS": "Space Exploration",   # AST SpaceMobile
    "LUNR": "Space Exploration",   # Intuitive Machines
    "SPCE": "Space Exploration",   # Virgin Galactic
    "PL": "Space Exploration",     # Planet Labs
    "BKSY": "Space Exploration",   # BlackSky
    "JOBY": "Space Exploration",   # eVTOL — adjacent
    "ACHR": "Space Exploration",   # Archer Aviation — adjacent
    "BA": "Space Exploration",     # Boeing — defense+space
    "LMT": "Space Exploration",    # Lockheed — defense+space

    # Quantum Computing
    "IONQ": "Quantum Computing",
    "RGTI": "Quantum Computing",   # Rigetti
    "QBTS": "Quantum Computing",   # D-Wave
    "QUBT": "Quantum Computing",   # Quantum Computing Inc
    "ARQQ": "Quantum Computing",   # Arqit Quantum
    "IBM": "Quantum Computing",    # also has classical biz but quantum-known

    # Crypto & Mining
    "COIN": "Crypto & Mining",
    "MSTR": "Crypto & Mining",
    "MARA": "Crypto & Mining",
    "RIOT": "Crypto & Mining",
    "HUT": "Crypto & Mining",
    "BITF": "Crypto & Mining",
    "CIFR": "Crypto & Mining",
    "CLSK": "Crypto & Mining",
    "CAN": "Crypto & Mining",
    "BTBT": "Crypto & Mining",
    "BTCS": "Crypto & Mining",
    "BITO": "Crypto & Mining",
    "GBTC": "Crypto & Mining",
    "ETHE": "Crypto & Mining",
    "IREN": "Crypto & Mining",     # Iris Energy — bitcoin miner pivoting to AI

    # Electric Vehicles
    "TSLA": "Electric Vehicles",
    "RIVN": "Electric Vehicles",
    "LCID": "Electric Vehicles",
    "NIO": "Electric Vehicles",
    "XPEV": "Electric Vehicles",
    "LI": "Electric Vehicles",
    "FSR": "Electric Vehicles",
    "MULN": "Electric Vehicles",
    "WKHS": "Electric Vehicles",
    "GOEV": "Electric Vehicles",

    # Autonomous & Robotics
    "MBLY": "Autonomous & Robotics",
    "AUR": "Autonomous & Robotics",  # Aurora Innovation
    "TSLL": "Autonomous & Robotics",  # 2x TSLA — robotaxi narrative

    # Biotech & Pharma
    "MRNA": "Biotech & Pharma",
    "BNTX": "Biotech & Pharma",
    "NVAX": "Biotech & Pharma",
    "REGN": "Biotech & Pharma",
    "VRTX": "Biotech & Pharma",
    "BIIB": "Biotech & Pharma",
    "GILD": "Biotech & Pharma",
    "AMGN": "Biotech & Pharma",
    "LLY": "Biotech & Pharma",
    "PFE": "Biotech & Pharma",
    "MRK": "Biotech & Pharma",
    "ABBV": "Biotech & Pharma",
    "BMY": "Biotech & Pharma",
    "JNJ": "Biotech & Pharma",
    "ATRA": "Biotech & Pharma",   # Atara Biotherapeutics
    "AUPH": "Biotech & Pharma",   # Aurinia Pharma
    "IBRX": "Biotech & Pharma",   # ImmunityBio
    "AGL": "Biotech & Pharma",    # agilon health
    "TLRY": "Biotech & Pharma",
    "SNDL": "Biotech & Pharma",

    # Fintech
    "SOFI": "Fintech",
    "AFRM": "Fintech",
    "UPST": "Fintech",
    "HOOD": "Fintech",
    "PYPL": "Fintech",
    "SQ": "Fintech",
    "PGY": "Fintech",   # Pagaya
    "OPEN": "Fintech",  # Opendoor

    # Cybersecurity
    "PANW": "Cybersecurity",
    "CRWD": "Cybersecurity",
    "ZS": "Cybersecurity",
    "NET": "Cybersecurity",
    "OKTA": "Cybersecurity",

    # Cloud & SaaS
    "DDOG": "Cloud & SaaS",
    "SNOW": "Cloud & SaaS",
    "MDB": "Cloud & SaaS",
    "NOW": "Cloud & SaaS",
    "CRM": "Cloud & SaaS",
    "ADBE": "Cloud & SaaS",
    "WDAY": "Cloud & SaaS",
    "INTU": "Cloud & SaaS",
    "TEAM": "Cloud & SaaS",
    "ZM": "Cloud & SaaS",
    "DOCU": "Cloud & SaaS",
    "SHOP": "Cloud & SaaS",
    "U": "Cloud & SaaS",  # Unity
    "FSLY": "Cloud & SaaS",
    "RBLX": "Cloud & SaaS",

    # Semiconductors (Other)
    "INTC": "Semiconductors (Other)",
    "QCOM": "Semiconductors (Other)",
    "TXN": "Semiconductors (Other)",
    "ON": "Semiconductors (Other)",
    "MCHP": "Semiconductors (Other)",
    "LRCX": "Semiconductors (Other)",
    "AMAT": "Semiconductors (Other)",
    "KLAC": "Semiconductors (Other)",
    "ASML": "Semiconductors (Other)",

    # Mega Cap Tech
    "AAPL": "Mega Cap Tech",
    "AMZN": "Mega Cap Tech",
    "NFLX": "Mega Cap Tech",
    "CSCO": "Mega Cap Tech",
    "HPQ": "Mega Cap Tech",

    # Consumer & Retail
    "WMT": "Consumer & Retail",
    "TGT": "Consumer & Retail",
    "COST": "Consumer & Retail",
    "HD": "Consumer & Retail",
    "LOW": "Consumer & Retail",
    "NKE": "Consumer & Retail",
    "SBUX": "Consumer & Retail",
    "MCD": "Consumer & Retail",
    "DIS": "Consumer & Retail",
    "KO": "Consumer & Retail",
    "PEP": "Consumer & Retail",
    "PG": "Consumer & Retail",
    "ULTA": "Consumer & Retail",
    "LULU": "Consumer & Retail",
    "ROST": "Consumer & Retail",
    "CMG": "Consumer & Retail",
    "DPZ": "Consumer & Retail",
    "SHAK": "Consumer & Retail",   # Shake Shack
    "WHR": "Consumer & Retail",    # Whirlpool
    "ABNB": "Consumer & Retail",
    "BKNG": "Consumer & Retail",
    "EXPE": "Consumer & Retail",
    "MAR": "Consumer & Retail",
    "HLT": "Consumer & Retail",

    # Energy
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "SLB": "Energy",
    "OXY": "Energy",
    "EOG": "Energy",
    "PSX": "Energy",
    "MPC": "Energy",
    "VLO": "Energy",
    "DVN": "Energy",
    "FANG": "Energy",
    "USO": "Energy",  # oil ETF
    "UNG": "Energy",
    "XLE": "Energy",

    # Defense & Aerospace
    "RTX": "Defense & Aerospace",
    "GD": "Defense & Aerospace",
    "NOC": "Defense & Aerospace",

    # Banks & Financials
    "JPM": "Banks & Financials",
    "BAC": "Banks & Financials",
    "WFC": "Banks & Financials",
    "C": "Banks & Financials",
    "GS": "Banks & Financials",
    "MS": "Banks & Financials",
    "SCHW": "Banks & Financials",
    "BLK": "Banks & Financials",
    "BX": "Banks & Financials",
    "V": "Banks & Financials",
    "MA": "Banks & Financials",
    "AXP": "Banks & Financials",
    "BRK.A": "Banks & Financials",
    "BRK.B": "Banks & Financials",

    # ETFs & Indexes
    "SPY": "ETFs & Indexes",
    "QQQ": "ETFs & Indexes",
    "IWM": "ETFs & Indexes",
    "DIA": "ETFs & Indexes",
    "VOO": "ETFs & Indexes",
    "VTI": "ETFs & Indexes",
    "ARKK": "ETFs & Indexes",
    "ARKG": "ETFs & Indexes",
    "TQQQ": "ETFs & Indexes",
    "SQQQ": "ETFs & Indexes",
    "TLT": "ETFs & Indexes",
    "GLD": "ETFs & Indexes",
    "SLV": "ETFs & Indexes",
    "UVXY": "ETFs & Indexes",
    "VXX": "ETFs & Indexes",

    # Meme & Retail-Favorite
    "GME": "Meme & Retail-Favorite",
    "AMC": "Meme & Retail-Favorite",
    "BB": "Meme & Retail-Favorite",
    "BBBY": "Meme & Retail-Favorite",
    "NOK": "Meme & Retail-Favorite",
    "WISH": "Meme & Retail-Favorite",
    "CLOV": "Meme & Retail-Favorite",
}


def get_sector(ticker: str) -> str:
    return TICKER_TO_SECTOR.get(ticker.upper(), "Other")


def group_by_sector(tickers: list, key=lambda t: t.ticker) -> dict[str, list]:
    """Group objects by sector. Returns dict in SECTOR_ORDER order.

    Sectors in ALWAYS_SHOW_SECTORS appear even when empty (with []), so the
    user's pinned themes (AI Chips, Space, Quantum, etc.) always show on the
    dashboard. Other sectors only appear when they have at least one ticker.
    """
    grouped: dict[str, list] = {}
    for t in tickers:
        sector = get_sector(key(t))
        grouped.setdefault(sector, []).append(t)

    ordered: dict[str, list] = {}
    for sector in SECTOR_ORDER:
        if sector in grouped:
            ordered[sector] = grouped[sector]
        elif sector in ALWAYS_SHOW_SECTORS:
            ordered[sector] = []
    # Catch any sectors not in SECTOR_ORDER (shouldn't happen, but safe).
    for sector, items in grouped.items():
        if sector not in ordered:
            ordered[sector] = items
    return ordered
