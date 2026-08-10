"""Closed production profiles for document-based service orders."""

from dataclasses import dataclass

from modules.service_orders.catalog import ServiceType


@dataclass(frozen=True, slots=True)
class DocumentServiceProfile:
    service_type: ServiceType
    artifact_stem: str

    @property
    def event_prefix(self) -> str:
        return f"service.{self.service_type.value}"

    @property
    def requested_event(self) -> str:
        return f"{self.event_prefix}.requested"

    @property
    def cleanup_event(self) -> str:
        return f"{self.event_prefix}.cleanup_requested"


DOCUMENT_SERVICE_PROFILES = {
    ServiceType.ESSAY: DocumentServiceProfile(
        service_type=ServiceType.ESSAY,
        artifact_stem="referat",
    ),
    ServiceType.INDEPENDENT_WORK: DocumentServiceProfile(
        service_type=ServiceType.INDEPENDENT_WORK,
        artifact_stem="mustaqil_ish",
    ),
    ServiceType.COURSE_WORK: DocumentServiceProfile(
        service_type=ServiceType.COURSE_WORK,
        artifact_stem="kurs_ishi",
    ),
    ServiceType.SUMMARY: DocumentServiceProfile(
        service_type=ServiceType.SUMMARY,
        artifact_stem="konspekt",
    ),
}
PRODUCTION_DOCUMENT_SERVICE_TYPES = frozenset(
    service_type.value for service_type in DOCUMENT_SERVICE_PROFILES
)


def document_service_profile(service_type: ServiceType | str) -> DocumentServiceProfile:
    canonical = ServiceType(service_type)
    try:
        return DOCUMENT_SERVICE_PROFILES[canonical]
    except KeyError as exc:
        raise ValueError("service type has no production document profile") from exc
