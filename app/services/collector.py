from app.core.config import Settings
from app.models.collector import CollectorState, LinkIngestRequest, LinkIngestResponse


class CollectorService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def get_state(self) -> CollectorState:
        return CollectorState(
            inbox_path=self.settings.inbox_path,
            history_file=self.settings.history_file,
            pending_links=self._line_count(self.settings.inbox_path),
            downloaded_ids=self._line_count(self.settings.history_file),
        )

    def ingest_links(self, payload: LinkIngestRequest) -> LinkIngestResponse:
        self.settings.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.inbox_path.open("a", encoding="utf-8") as inbox:
            for url in payload.urls:
                inbox.write(f"{url}\n")
        return LinkIngestResponse(accepted=len(payload.urls), inbox_path=self.settings.inbox_path)

    @staticmethod
    def _line_count(path) -> int:
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as file:
            return sum(1 for line in file if line.strip())

