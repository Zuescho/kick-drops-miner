"""Kick.com API helpers over a long-lived ``Browser``.

All endpoints are fetched in-page from the kick.com origin (the Browser stays on
https://kick.com) so Cloudflare clearance and credentials apply. Every method
returns None / [] on failure and never raises.
"""
from urllib.parse import urlparse


def channel_slug_from_url(url):
    """'https://kick.com/foo' -> 'foo'; non-kick or invalid -> None.
    Also accepts a bare slug 'foo' -> 'foo'."""
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None
    if "://" not in s and "/" not in s and "." not in s:
        # Bare slug.
        return s or None
    try:
        p = urlparse(s if "://" in s else "https://" + s)
    except Exception:
        return None
    if "kick.com" not in (p.netloc or ""):
        return None
    slug = (p.path or "").strip("/").split("/")[0]
    return slug or None


# web.kick.com drops endpoints
_CAMPAIGNS_URL = "https://web.kick.com/api/v1/drops/campaigns"
_PROGRESS_URL = "https://web.kick.com/api/v1/drops/progress"


def _channel_url(slug):
    return f"https://kick.com/api/v2/channels/{slug}"


def _livestreams_url(category_id, limit):
    return (
        "https://web.kick.com/api/v1/livestreams"
        f"?limit={limit}&sort=viewer_count_desc&category_id={category_id}"
    )


class KickClient:
    """Stateless helpers over a Browser. All methods return None / [] on
    failure, never raise."""

    def __init__(self, browser, log):
        self._b = browser
        self._log = log

    # -- channel live / category ------------------------------------------

    def channel_info(self, channel):
        """Raw v2 channels payload (dict) or None."""
        slug = channel_slug_from_url(channel)
        if not slug:
            return None
        data = self._b.fetch_json(_channel_url(slug))
        if not isinstance(data, dict):
            return None
        return data

    def is_live(self, channel):
        """True/False if known, None if unknown (network/parse error). Reads
        livestream.is_live from v2 channels."""
        data = self.channel_info(channel)
        if data is None:
            return None
        livestream = data.get("livestream")
        return bool(livestream and livestream.get("is_live"))

    def current_category_id(self, channel):
        """livestream.categories[0].id when live, else None."""
        data = self.channel_info(channel)
        if not isinstance(data, dict):
            return None
        livestream = data.get("livestream")
        if not (livestream and livestream.get("is_live")):
            return None
        categories = livestream.get("categories") or []
        if categories:
            try:
                return categories[0].get("id")
            except Exception:
                return None
        return None

    def required_category_present(self, channel, required_id):
        """Whether ``required_id`` is among the channel's CURRENT live
        categories. True/False when known; None if unknown, offline, or the
        category list is momentarily empty (a transition we must not penalize).
        Checks ALL categories, not just the first."""
        data = self.channel_info(channel)
        if data is None:
            return None
        livestream = data.get("livestream")
        if not (livestream and livestream.get("is_live")):
            return None
        categories = livestream.get("categories") or []
        ids = {c.get("id") for c in categories if isinstance(c, dict)}
        if not ids:
            return None
        return required_id in ids

    # -- drops campaigns / progress ---------------------------------------

    def fetch_campaigns(self, bearer=None):
        """Normalized campaigns. Each: {id, name, game, game_slug, game_image,
        status, starts_at, ends_at, rewards, category_id, channels:[{slug,url}]}.
        category_id is best-effort (campaign.category.id if present)."""
        data = self._b.fetch_json(_CAMPAIGNS_URL, bearer=bearer)
        if not isinstance(data, dict):
            return []
        rows = data.get("data")
        if not isinstance(rows, list):
            return []

        campaigns = []
        for campaign in rows:
            if not isinstance(campaign, dict):
                continue
            category = campaign.get("category") or {}
            if not isinstance(category, dict):
                category = {}
            info = {
                "id": campaign.get("id"),
                "name": campaign.get("name", "Unknown Campaign"),
                "game": category.get("name", "Unknown Game"),
                "game_slug": category.get("slug", ""),
                "game_image": category.get("image_url", ""),
                "status": campaign.get("status", "unknown"),
                "starts_at": campaign.get("starts_at"),
                "ends_at": campaign.get("ends_at"),
                "rewards": campaign.get("rewards", []),
                "category_id": category.get("id"),
                "channels": [],
            }
            for channel in campaign.get("channels") or []:
                if not isinstance(channel, dict):
                    continue
                slug = channel.get("slug")
                if not slug:
                    user = channel.get("user") or {}
                    slug = user.get("username") or user.get("slug")
                if slug:
                    info["channels"].append(
                        {"slug": slug, "url": f"https://kick.com/{slug}"}
                    )
            campaigns.append(info)
        return campaigns

    def fetch_progress(self, bearer=None):
        """Raw progress 'data' list (dicts). [] on failure."""
        data = self._b.fetch_json(_PROGRESS_URL, bearer=bearer)
        if not isinstance(data, dict):
            return []
        rows = data.get("data")
        return rows if isinstance(rows, list) else []

    # -- livestreams by category ------------------------------------------

    def live_streamers_by_category(self, category_id, limit=24):
        """Channel URLs ('https://kick.com/{slug}') currently live in a
        category."""
        if not category_id:
            return []
        data = self._b.fetch_json(_livestreams_url(category_id, limit))
        if not isinstance(data, dict):
            return []

        # Response shapes seen: {"data": {"livestreams": [...]}} or {"data": [...]}.
        data_obj = data.get("data")
        if isinstance(data_obj, dict):
            streams = data_obj.get("livestreams") or []
        elif isinstance(data_obj, list):
            streams = data_obj
        else:
            streams = []

        urls = []
        for stream in streams[:limit]:
            if not isinstance(stream, dict):
                continue
            channel = stream.get("channel") or {}
            slug = channel.get("slug")
            if not slug:
                user = channel.get("user") or {}
                slug = user.get("username") or user.get("slug")
            if slug:
                urls.append(f"https://kick.com/{slug}")
        return urls
