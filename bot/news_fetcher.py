"""
bot/news_fetcher.py — Busca e pontua notícias para o News Edge.

Fontes suportadas:
    1. NewsAPI (newsapi.org) — requer chave gratuita (NEWS_API_KEY no .env)
       Free tier: 100 req/dia, artigos com até 1 mês de histórico.

    2. RSS feeds públicos — zero autenticação, zero limite de requisições.
       Fontes padrão: Reuters, CoinDesk, CryptoPanic.

Sem novas dependências: usa apenas `requests` (já no requirements.txt)
e `xml.etree.ElementTree` da stdlib para parsing de RSS.

Scoring de sentimento:
    Não usa NLP pesado — apenas matching de palavras-chave ponderadas.
    Suficiente para mercados de predição onde o impacto de notícias é
    frequentemente binário (evento ocorreu / não ocorreu).

    Score > +0.3  → sinal bullish
    Score < -0.3  → sinal bearish
    Entre ±0.3    → neutro / HOLD
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_NEWSAPI_URL = "https://newsapi.org/v2/everything"

# RSS feeds públicos gratuitos, sem autenticação
DEFAULT_RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptopanic.com/news/rss/",
]

# Palavras com peso positivo (bullish)
_BULLISH_WORDS = {
    "approved": 2, "approval": 2, "win": 2, "won": 2, "victory": 2,
    "elected": 2, "confirmed": 2, "passed": 2, "positive": 1,
    "surge": 1, "rally": 1, "gain": 1, "rise": 1, "up": 0.5,
    "bullish": 2, "beat": 1, "exceed": 1, "above": 0.5, "launch": 1,
    "partnership": 1, "deal": 1, "agreement": 1, "record": 1,
}

# Palavras com peso negativo (bearish)
_BEARISH_WORDS = {
    "rejected": 2, "rejection": 2, "lost": 2, "loss": 2, "defeat": 2,
    "failed": 2, "banned": 2, "ban": 2, "crash": 2, "collapse": 2,
    "fraud": 2, "hack": 2, "sued": 2, "lawsuit": 2, "investigation": 1,
    "bearish": 2, "drop": 1, "fall": 1, "decline": 1, "down": 0.5,
    "delay": 1, "delayed": 1, "cancel": 1, "cancelled": 1, "risk": 0.5,
    "concern": 0.5, "warning": 1, "negative": 1, "miss": 1, "below": 0.5,
}


@dataclass
class NewsArticle:
    """
    Representa um artigo de notícia com score de sentimento calculado.

    Attributes:
        title:       Título do artigo.
        description: Resumo ou primeiro parágrafo.
        url:         URL original (apenas para logging — não acessado).
        published:   Timestamp de publicação (Unix).
        source:      Nome da fonte (NewsAPI ou nome do feed RSS).
        score:       Score de sentimento: positivo = bullish, negativo = bearish.
        keywords_matched: Palavras-chave do mercado encontradas no artigo.
    """
    title: str
    description: str
    url: str
    published: float
    source: str
    score: float = 0.0
    keywords_matched: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        direction = "↑" if self.score > 0 else ("↓" if self.score < 0 else "→")
        return f"NewsArticle({direction}{self.score:+.2f} [{self.source}]: {self.title[:60]}…)"


class NewsFetcher:
    """
    Busca e pontua artigos de notícias relevantes para um mercado.

    Combina NewsAPI (se chave disponível) e feeds RSS públicos.
    Filtra artigos por relevância usando palavras-chave do mercado
    e calcula score de sentimento via keyword matching.

    Args:
        market_keywords: Lista de termos para filtrar artigos relevantes.
                         Ex: ["bitcoin", "BTC", "crypto"] para mercados BTC.
        news_api_key:    Chave do NewsAPI. Se None, usa apenas RSS.
        max_age_hours:   Ignora artigos mais velhos que N horas (padrão: 4).
        cache_ttl_sec:   Segundos de cache para evitar refetch (padrão: 300).
        rss_feeds:       Lista de URLs RSS. Usa DEFAULT_RSS_FEEDS se None.
    """

    def __init__(
        self,
        market_keywords: List[str],
        news_api_key: Optional[str] = None,
        max_age_hours: float = 4.0,
        cache_ttl_sec: int = 300,
        rss_feeds: Optional[List[str]] = None,
    ):
        self.market_keywords = [kw.lower() for kw in market_keywords]
        self.news_api_key = news_api_key or os.getenv("NEWS_API_KEY", "")
        self.max_age_sec = max_age_hours * 3600
        self.cache_ttl_sec = cache_ttl_sec
        self.rss_feeds = rss_feeds or DEFAULT_RSS_FEEDS

        self._cache: List[NewsArticle] = []
        self._cache_at: float = 0.0

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polymarket-trading-bot/1.0"})

    # ------------------------------------------------------------------
    # Interface principal
    # ------------------------------------------------------------------

    def get_sentiment_score(self) -> float:
        """
        Retorna o score agregado de sentimento dos artigos recentes.

        Score é a média ponderada dos artigos relevantes encontrados,
        normalizada entre -1.0 e +1.0.

        Returns:
            Float entre -1.0 (muito bearish) e +1.0 (muito bullish).
            0.0 se nenhum artigo relevante foi encontrado.
        """
        articles = self._fetch_all()
        if not articles:
            return 0.0

        relevant = [a for a in articles if a.keywords_matched]
        if not relevant:
            logger.debug("[NewsFetcher] Nenhum artigo relevante encontrado")
            return 0.0

        total_score = sum(a.score for a in relevant)
        avg_score = total_score / len(relevant)
        normalized = max(-1.0, min(1.0, avg_score))

        logger.info(
            f"[NewsFetcher] {len(relevant)} artigos relevantes | "
            f"score médio={avg_score:+.3f}"
        )
        return normalized

    def get_recent_articles(self) -> List[NewsArticle]:
        """Retorna artigos relevantes do cache atual (para logging/debug)."""
        return [a for a in self._fetch_all() if a.keywords_matched]

    # ------------------------------------------------------------------
    # Fetching e parsing
    # ------------------------------------------------------------------

    def _fetch_all(self) -> List[NewsArticle]:
        """Busca artigos de todas as fontes com cache."""
        now = time.time()
        if self._cache and now - self._cache_at < self.cache_ttl_sec:
            return self._cache

        articles: List[NewsArticle] = []

        if self.news_api_key:
            articles.extend(self._fetch_newsapi())

        for feed_url in self.rss_feeds:
            articles.extend(self._fetch_rss(feed_url))

        # Filtrar por idade
        cutoff = now - self.max_age_sec
        articles = [a for a in articles if a.published >= cutoff]

        # Calcular scores e relevância
        for article in articles:
            self._score_article(article)

        self._cache = articles
        self._cache_at = now
        return articles

    def _fetch_newsapi(self) -> List[NewsArticle]:
        """Busca artigos via NewsAPI.org."""
        query = " OR ".join(self.market_keywords[:5])  # API aceita até ~5 termos bem
        try:
            resp = self._session.get(
                _NEWSAPI_URL,
                params={
                    "q": query,
                    "sortBy": "publishedAt",
                    "pageSize": 20,
                    "language": "en",
                    "apiKey": self.news_api_key,
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()

            articles = []
            for item in data.get("articles", []):
                published = self._parse_iso_timestamp(item.get("publishedAt", ""))
                articles.append(NewsArticle(
                    title=item.get("title", "") or "",
                    description=item.get("description", "") or "",
                    url=item.get("url", ""),
                    published=published,
                    source=item.get("source", {}).get("name", "newsapi"),
                ))
            logger.debug(f"[NewsFetcher] NewsAPI: {len(articles)} artigos")
            return articles

        except requests.RequestException as e:
            logger.warning(f"[NewsFetcher] Erro NewsAPI: {e}")
            return []

    def _fetch_rss(self, feed_url: str) -> List[NewsArticle]:
        """Busca e parseia um feed RSS público."""
        try:
            resp = self._session.get(feed_url, timeout=5)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            articles = []
            # Suporte a RSS 2.0 e Atom
            items = root.findall(".//item") or root.findall(
                ".//{http://www.w3.org/2005/Atom}entry"
            )
            for item in items[:20]:
                title = (
                    _xml_text(item, "title")
                    or _xml_text(item, "{http://www.w3.org/2005/Atom}title")
                    or ""
                )
                description = (
                    _xml_text(item, "description")
                    or _xml_text(item, "{http://www.w3.org/2005/Atom}summary")
                    or ""
                )
                pub_str = (
                    _xml_text(item, "pubDate")
                    or _xml_text(item, "{http://www.w3.org/2005/Atom}published")
                    or ""
                )
                link = _xml_text(item, "link") or ""
                published = self._parse_rfc_timestamp(pub_str)

                articles.append(NewsArticle(
                    title=title,
                    description=description,
                    url=link,
                    published=published,
                    source=feed_url.split("/")[2],  # domínio como nome da fonte
                ))

            logger.debug(f"[NewsFetcher] RSS {feed_url.split('/')[2]}: {len(articles)} artigos")
            return articles

        except Exception as e:
            logger.debug(f"[NewsFetcher] Erro RSS {feed_url}: {e}")
            return []

    # ------------------------------------------------------------------
    # Scoring de sentimento
    # ------------------------------------------------------------------

    def _score_article(self, article: NewsArticle) -> None:
        """
        Calcula o score de sentimento e relevância do artigo in-place.

        Primeiro verifica relevância (keywords do mercado).
        Depois aplica keyword matching bullish/bearish no texto completo.
        """
        text = (article.title + " " + article.description).lower()
        words = text.split()

        # Relevância: quantas keywords do mercado aparecem no texto
        matched = [kw for kw in self.market_keywords if kw in text]
        article.keywords_matched = matched

        if not matched:
            article.score = 0.0
            return

        # Scoring de sentimento via keyword matching
        raw_score = 0.0
        for word in words:
            clean = word.strip(".,!?;:'\"()")
            if clean in _BULLISH_WORDS:
                raw_score += _BULLISH_WORDS[clean]
            elif clean in _BEARISH_WORDS:
                raw_score -= _BEARISH_WORDS[clean]

        # Normalizar pelo número de palavras (evitar artigos longos dominarem)
        if len(words) > 0:
            raw_score = raw_score / (len(words) ** 0.5)

        # Amplificar pelo número de keywords do mercado encontradas
        relevance_boost = 1 + (len(matched) - 1) * 0.2
        article.score = raw_score * relevance_boost

    # ------------------------------------------------------------------
    # Parsing de timestamps
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_iso_timestamp(iso_str: str) -> float:
        """Converte string ISO 8601 para Unix timestamp."""
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return time.time()

    @staticmethod
    def _parse_rfc_timestamp(rfc_str: str) -> float:
        """Converte string RFC 2822 (RSS pubDate) para Unix timestamp."""
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(rfc_str)
            return dt.timestamp()
        except Exception:
            return time.time()


def _xml_text(element: ET.Element, tag: str) -> Optional[str]:
    """Extrai texto de um elemento XML filho, retorna None se não encontrar."""
    child = element.find(tag)
    return child.text if child is not None else None
