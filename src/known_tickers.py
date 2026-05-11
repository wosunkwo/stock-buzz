"""Curated list of liquid US-listed tickers we care about.

Phase 1 goal: cover the tickers people actually discuss on r/wallstreetbets,
r/stocks, r/investing, etc. — large caps, popular meme stocks, common ETFs.

This is intentionally hand-curated, not exhaustive. The point is to filter
out false positives like "DD", "USA", "CEO", "YOLO" from the regex match,
not to cover every listed security. We can expand later by ingesting a full
NASDAQ/NYSE listing.
"""

KNOWN_TICKERS: set[str] = {
    # FAANG + mega caps
    "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NFLX", "NVDA", "TSLA",
    "AVGO", "ORCL", "CRM", "ADBE", "AMD", "INTC", "QCOM", "MU", "TXN", "IBM",
    "CSCO", "DELL", "HPQ", "PANW", "CRWD", "ZS", "NET", "DDOG", "SNOW", "MDB",
    "PLTR", "NOW", "WDAY", "INTU", "TEAM", "ZM", "DOCU", "OKTA", "SHOP",

    # Semis / hardware
    "ASML", "TSM", "ARM", "MRVL", "ON", "MCHP", "LRCX", "AMAT", "KLAC", "SMCI",

    # Finance / banks
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "BX", "KKR", "AXP",
    "V", "MA", "PYPL", "SQ", "COF", "USB", "PNC", "TFC", "BRK.A", "BRK.B",

    # Consumer / retail
    "WMT", "TGT", "COST", "HD", "LOW", "NKE", "SBUX", "MCD", "DIS", "KO",
    "PEP", "PG", "CL", "UL", "MDLZ", "PM", "MO", "BUD", "STZ", "DEO",
    "CMG", "QSR", "DPZ", "YUM", "ULTA", "TJX", "LULU", "ROST",

    # Auto / EV / mobility
    "F", "GM", "STLA", "TM", "HMC", "RIVN", "LCID", "NIO", "XPEV", "LI",
    "FSR", "MULN", "WKHS", "GOEV", "CVNA", "KMX", "UBER", "LYFT", "DASH",

    # Energy
    "XOM", "CVX", "COP", "SLB", "OXY", "EOG", "PSX", "MPC", "VLO", "PXD",
    "DVN", "FANG", "HES", "HAL", "BKR", "RIG", "NE", "TRGP", "WMB", "KMI",

    # Healthcare / pharma / biotech
    "JNJ", "PFE", "MRK", "ABBV", "LLY", "BMY", "GILD", "AMGN", "REGN", "VRTX",
    "BIIB", "MRNA", "BNTX", "NVAX", "MDT", "ABT", "TMO", "DHR", "ISRG", "SYK",
    "BSX", "EW", "ZTS", "CI", "ELV", "UNH", "HUM", "CVS", "WBA",

    # Industrials / aerospace / defense
    "BA", "LMT", "RTX", "GD", "NOC", "GE", "HON", "MMM", "CAT", "DE",
    "EMR", "ETN", "ITW", "ROK", "PH", "IR", "FDX", "UPS", "CSX", "UNP",
    "NSC", "DAL", "UAL", "AAL", "LUV", "JBLU",

    # Media / telecom / streaming
    "T", "VZ", "TMUS", "CMCSA", "CHTR", "WBD", "PARA", "FOX", "FOXA",
    "ROKU", "FUBO", "SIRI", "SPOT", "EA", "TTWO", "ATVI", "RBLX", "U",

    # Crypto-adjacent
    "COIN", "MSTR", "MARA", "RIOT", "HOOD", "HUT", "BITF", "CIFR", "CLSK",
    "CAN", "BTBT", "BTCS", "BITO", "GBTC", "ETHE",

    # Travel / leisure
    "ABNB", "BKNG", "EXPE", "MAR", "HLT", "CCL", "RCL", "NCLH", "DKNG",
    "PENN", "MGM", "WYNN", "LVS",

    # Meme / retail-favorite tickers
    "GME", "AMC", "BB", "BBBY", "NOK", "WISH", "CLOV", "SPCE", "TLRY",
    "SNDL", "SOFI", "OPEN", "UPST", "AFRM", "RKLB", "SOUN", "AI",

    # IPOs / newer hot names
    "SNAP", "PINS", "TWLO", "PATH", "BIRD", "POSH", "OPRA", "DUOL", "RDDT",
    "ASTS", "JOBY", "ACHR", "BMBL", "BUMBLE", "CART", "ABNB", "FROG",
    "CRWV",   # CoreWeave — AI cloud
    "NBIS",   # Nebius — AI infra
    "RKLB",   # Rocket Lab — already in EVs but most people see it as space
    "RDDT", "TOST", "HUBS", "TTD", "AAOI", "INOD", "RCAT", "TSSI",

    # Quantum Computing — the headline names that show up in chatter
    "IONQ",   # IonQ
    "RGTI",   # Rigetti Computing
    "QBTS",   # D-Wave Quantum
    "QUBT",   # Quantum Computing Inc
    "ARQQ",   # Arqit Quantum
    "QMCO",   # Quantum Corporation (storage but quantum-named)

    # Space Exploration
    "LUNR",   # Intuitive Machines — moon lander
    "PL",     # Planet Labs — earth imaging satellites
    "BKSY",   # BlackSky — geospatial intel
    "SPCE",   # Virgin Galactic

    # AI Datacenter / Power adjacency
    "VRT",    # Vertiv — datacenter cooling
    "GEV",    # GE Vernova — power infra
    "CEG",    # Constellation Energy — nuclear datacenter power
    "VST",    # Vistra Energy — datacenter power
    "TLN",    # Talen Energy — Amazon nuclear deal

    # SMR / Nuclear (often discussed alongside AI power)
    "SMR",    # NuScale Power
    "OKLO",   # Oklo
    "NNE",    # Nano Nuclear

    # Misc tickers seen in StockTwits trending
    "MP",     # MP Materials — rare earth
    "USO",    # WTI ETF
    "KODK",   # Eastman Kodak
    "GRPN",   # Groupon
    "FIGS",   # FIGS scrubs
    "TSLL",   # 2x TSLA leveraged
    "MBLY",   # Mobileye
    "AUR",    # Aurora Innovation

    # ETFs
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "ARKK", "ARKG", "ARKW",
    "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE",
    "TLT", "IEF", "GLD", "SLV", "USO", "UNG", "UVXY", "VXX", "SQQQ", "TQQQ",
    "SOXX", "SMH", "SOXL", "SOXS", "TSLL", "NVDL",

    # International (US-traded ADRs that pop up on these subs)
    "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV", "LI", "TCEHY", "TM",
    "SONY", "RACE", "STLA", "RIO", "BHP", "VALE", "PBR", "ITUB", "BBD",
}
