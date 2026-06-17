from __future__ import annotations

from threading import local

from llama_index.llms.ollama import Ollama


def resolve_parallel_workers(task_count: int, configured: int | None = None, default: int = 4) -> int:
    if task_count <= 1:
        return 1
    worker_limit = configured if configured and configured > 0 else default
    return max(1, min(task_count, worker_limit))


class ParallelOllamaRuntime:
    def __init__(
        self,
        model_name: str,
        base_url: str,
        *,
        request_timeout: float = 1000.0,
    ) -> None:
        self.model_name = model_name
        self.base_url = base_url
        self.request_timeout = request_timeout
        self._local = local()

    def client(self) -> Ollama:
        llm = getattr(self._local, "llm", None)
        if llm is None:
            llm = Ollama(
                model=self.model_name,
                base_url=self.base_url,
                request_timeout=self.request_timeout,
            )
            self._local.llm = llm
        return llm

    def complete(self, prompt: str) -> str:
        return str(self.client().complete(prompt))
