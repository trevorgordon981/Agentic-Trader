import rag_news_fetch as news


def test_short_tickers_require_company_name_or_explicit_cashtag():
    assert not news.ticker_relevant("A", "A major chipmaker raises guidance")
    assert news.ticker_relevant("A", "Agilent raises its full-year outlook")
    assert news.ticker_relevant("A", "$A breaks above resistance")
    assert not news.ticker_relevant("V", "Company reports a V-shaped recovery")
    assert news.ticker_relevant("V", "Visa expands tokenized payments")
    assert news.ticker_relevant("V", "$V option volume jumps")


def test_long_tickers_keep_boundary_matching():
    assert news.ticker_relevant("AAPL", "AAPL launches a new device")
    assert not news.ticker_relevant("AAPL", "XAAPLX is an unrelated token")
