from __future__ import annotations
        self._warn_size = crawler.settings.getint("DOWNLOAD_WARNSIZE")
        crawler.signals.connect(self.open_spider, signals.spider_opened)


    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        if not crawler.settings.getbool("COMPRESSION_ENABLED"):
            raise NotConfigured
        return cls(crawler=crawler)


    def open_spider(self, spider: Spider) -> None:
        if hasattr(spider, "download_maxsize"):
            warn_on_deprecated_spider_attribute("download_maxsize", "DOWNLOAD_MAXSIZE")
            self._max_size = spider.download_maxsize
        if hasattr(spider, "download_warnsize"):
            warn_on_deprecated_spider_attribute(
                "download_warnsize", "DOWNLOAD_WARNSIZE"
            )
            self._warn_size = spider.download_warnsize


    @_warn_spider_arg
    def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> Request | Response | None:
        request.headers.setdefault("Accept-Encoding", b", ".join(ACCEPTED_ENCODINGS))
        return None


    @_warn_spider_arg
    def process_response(
        self, request: Request, response: Response, spider: Spider | None = None
    ) -> Request | Response:
        if request.method == "HEAD":
            return response
        if isinstance(response, Response):
            content_encoding = response.headers.getlist("Content-Encoding")
            if content_encoding:
                # Preserve the original Content-Encoding so spiders can
                # see what encoding the server used (see #1988).
                original_encoding = b", ".join(content_encoding).decode(
                    "ascii", errors="replace"
                )
                max_size = request.meta.get("download_maxsize", self._max_size)
                warn_size = request.meta.get("download_warnsize", self._warn_size)
                try:
                    decoded_body, content_encoding = self._handle_encoding(
                        response.body, content_encoding, max_size
                    )
                except _DecompressionMaxSizeExceeded as e:
                    raise IgnoreRequest(
                        f"Ignored response {response} because its body "
                        f"({len(response.body)} B compressed, "
                        f"{e.decompressed_size} B decompressed so far) exceeded "
                        f"DOWNLOAD_MAXSIZE ({max_size} B) during decompression."
                    ) from e
                if len(response.body) < warn_size <= len(decoded_body):
                    logger.warning(
                        f"{response} body size after decompression "
                        f"({len(decoded_body)} B) is larger than the "
                        f"download warning size ({warn_size} B)."
                    )
                if content_encoding:
                    self._warn_unknown_encoding(response, content_encoding)
                response.headers["Content-Encoding"] = content_encoding
                if self.stats:
                    self.stats.inc_value(
                        "httpcompression/response_bytes",
                        len(decoded_body),
                    )
                    self.stats.inc_value("httpcompression/response_count")
                respcls = responsetypes.from_args(
                    headers=response.headers, url=response.url, body=decoded_body
                )
                kwargs: dict[str, Any] = {"body": decoded_body}
                if issubclass(respcls, TextResponse):
                    # force recalculating the encoding until we make sure the
                    # responsetypes guessing is reliable
                    kwargs["encoding"] = None
                response = response.replace(cls=respcls, **kwargs)
                if not content_encoding:
                    del response.headers["Content-Encoding"]
                # Store the original Content-Encoding value so spider
                # callbacks can access it via response.meta.
                response.meta["original_content_encoding"] = original_encoding


        return response


    def _handle_encoding(
        self, body: bytes, content_encoding: list[bytes], max_size: int
    ) -> tuple[bytes, list[bytes]]:
        to_decode, to_keep = self._split_encodings(content_encoding)
        for encoding in to_decode:
            body = self._decode(body, encoding, max_size)
        return body, to_keep


    @staticmethod
    def _split_encodings(
        content_encoding: list[bytes],
    ) -> tuple[list[bytes], list[bytes]]:
        supported_encodings = {*ACCEPTED_ENCODINGS, b"x-gzip"}
        to_keep: list[bytes] = [
            encoding.strip().lower()
            for encoding in chain.from_iterable(
                encodings.split(b",") for encodings in content_encoding
            )
        ]
        to_decode: list[bytes] = []
