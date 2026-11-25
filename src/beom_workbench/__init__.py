"""beom-workbench — 오픈소스 저장소 전수 분석 MCP 툴킷."""

__version__ = "0.3.1"


def _os_trust() -> None:
    """HTTPS 를 운영체제의 인증서 저장소로 확인한다 — 파이썬 기본 저장소로는 회사망·백신이 끼운 인증서나 빠진 중간 인증서 때문에
    공개 페이지(example.com)까지 'CERTIFICATE_VERIFY_FAILED' 로 못 읽었다 (링크 첨부·도구 받기·업데이트 확인 모두). 브라우저와 같은 판단."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 — 없으면 파이썬 기본 저장소 그대로
        pass


_os_trust()

