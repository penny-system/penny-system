def compute_features_from_bars(bars):
    """
    bars: list of 1-min bars for 1 trading day (useRTH=True)
    Output includes:
      - ref_price (last close)
      - ret_1 (approx last ~60 minutes vs prior)
      - ret_5 (proxy using first hour vs last hour; v1 simplification)
      - vol_surge (last hour volume / avg hourly volume)
      - dollar_vol (sum(volume) * ref_price)
      - breakout (1 if last close near day's high)
    """
    if not bars or len(bars) < 80:
        return None

    closes = [float(b.close) for b in bars if b.close is not None]
    vols = [float(b.volume) for b in bars if b.volume is not None]

    if len(closes) < 80 or len(vols) < 80:
        return None

    ref_price = closes[-1]
    day_high = max(closes)
    day_low = min(closes)

    # --- Returns ---
    # ret_1: last 60 mins vs previous 60 mins (approx)
    if len(closes) >= 120:
        p_now = closes[-1]
        p_prev = closes[-61]
        ret_1 = (p_now - p_prev) / p_prev if p_prev else 0.0
    else:
        ret_1 = 0.0

    # ret_5 proxy in v1: last hour vs first hour
    p_first = closes[60] if len(closes) > 60 else closes[0]
    p_last = closes[-1]
    ret_5 = (p_last - p_first) / p_first if p_first else 0.0

    # --- Volume surge ---
    # Compare last hour volume to average hourly volume
    hour = 60
    last_hour_vol = sum(vols[-hour:]) if len(vols) >= hour else sum(vols)
    total_vol = sum(vols)
    hours = max(1, len(vols) // hour)
    avg_hour_vol = total_vol / hours if hours else total_vol
    vol_surge = (last_hour_vol / avg_hour_vol) if avg_hour_vol else 1.0

    # --- Dollar volume (turnover proxy) ---
    dollar_vol = total_vol * ref_price

    # --- Breakout proxy: close in top 20% of day's range ---
    rng = (day_high - day_low)
    breakout = 0
    if rng > 0:
        if (ref_price - day_low) / rng >= 0.80:
            breakout = 1

    return {
        "ref_price": float(ref_price),
        "day_high": float(day_high),
        "day_low": float(day_low),
        "ret_1": float(ret_1),
        "ret_5": float(ret_5),
        "vol_surge": float(vol_surge),
        "dollar_vol": float(dollar_vol),
        "breakout": int(breakout),
    }
