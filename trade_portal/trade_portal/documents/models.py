import json
import logging
import mimetypes
import os
import random
import string
import uuid
from base64 import b64encode
from urllib.parse import quote

from django.conf import settings
from django.contrib.postgres.fields import JSONField
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_countries.fields import CountryField

from trade_portal.utils.qr import get_qrcode_image
from trade_portal.utils.monitoring import statsd_timer

logger = logging.getLogger(__name__)


class FTA(models.Model):
    # the Free Trading Agreement, per multiple countries (but may be just 2 of them)
    name = models.CharField(
        max_length=256, blank=True, default="", verbose_name=_("name")
    )
    country = CountryField(multiple=True, verbose_name=_("country"))

    def __str__(self):
        return self.name

    class Meta:
        ordering = ("name",)
        verbose_name = "FTA"
        verbose_name_plural = "FTAs"


class Party(models.Model):
    # This is business field, just a dictionary of values to click on
    # and bears no access right meaning (apart of business ID, which
    # gives access to organisations owning it)
    TYPE_TRADER = "t"
    TYPE_CHAMBERS = "c"
    TYPE_OTHER = "o"

    TYPE_CHOICES = (
        (TYPE_TRADER, _("Trader")),
        (TYPE_CHAMBERS, _("Chambers")),
        (TYPE_OTHER, _("Other")),
    )

    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.SET_NULL,
        related_name="parties_created",
        blank=True,
        null=True,
    )
    created_by_org = models.ForeignKey(
        "users.Organisation",
        models.SET_NULL,
        related_name="parties",
        blank=True,
        null=True,
    )

    type = models.CharField(
        max_length=1, blank=True, choices=TYPE_CHOICES, default=TYPE_OTHER
    )
    bid_prefix = models.CharField(max_length=64, blank=True, default="")
    clear_business_id = models.CharField(max_length=128, blank=True, default="")
    business_id = models.CharField(
        max_length=256, help_text=_("ABN or UEN for example"), blank=True
    )
    dot_separated_id = models.CharField(max_length=256, blank=True, default="")
    name = models.CharField(max_length=256, blank=True)
    country = CountryField(blank=True)

    postcode = models.CharField(max_length=12, blank=True, default="")
    line1 = models.CharField(max_length=255, blank=True, default="")
    line2 = models.CharField(max_length=255, blank=True, default="")
    city_name = models.CharField(max_length=255, blank=True, default="")
    countrySubDivisionName = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"{self.name} {self.business_id} {self.country}".strip()

    class Meta:
        ordering = ("name",)
        verbose_name = _("party")
        verbose_name_plural = _("parties")

    def save(self, *args, **kwargs):
        if (
            ":" in self.business_id
            and not self.bid_prefix
            and not self.clear_business_id
        ):
            self.bid_prefix, self.clear_business_id = self.business_id.rsplit(
                ":", maxsplit=1
            )
        else:
            if not self.clear_business_id and ":" not in self.business_id:
                self.clear_business_id = self.business_id
        super().save(*args, **kwargs)

    @property
    def contact_info(self):
        data = [
            f"{self.line1}" if self.line1 else "",
            f"{self.line2}" if self.line2 else "",
            f"{self.city_name}" if self.city_name else "",
            f"{self.postcode}" if self.postcode else "",
            f"{self.countrySubDivisionName}" if self.countrySubDivisionName else "",
            self.country.name if self.country else "",
        ]
        return ", ".join((v for v in data if v.strip()))

    @property
    def full_business_id(self):
        if self.country == settings.ICL_APP_COUNTRY:
            return f"{settings.BID_PREFIX}:{self.business_id}"
        else:
            return self.business_id

    @property
    def register_url(self):
        pure_business_id = self.business_id
        if ":" in pure_business_id:
            pure_business_id = pure_business_id.split(":")[-1]
        URLS = {
            "abr.gov.au:abn": f"https://abr.business.gov.au/ABN/View?abn={pure_business_id}",
            "gov.sg:UEN": "https://www.uen.gov.sg/",
        }
        normalized_business_id = self.full_business_id
        for bid_prefix, url in URLS.items():
            if normalized_business_id.startswith(bid_prefix):
                return url
        return None

    @property
    def readable_identifier_name(self):
        URLS = {
            "abr.gov.au:abn": "ABN",
            "gov.sg:UEN": "UEN",
        }
        normalized_business_id = self.full_business_id
        for bid_prefix, type_value in URLS.items():
            if normalized_business_id.startswith(bid_prefix):
                return type_value
        return _("Government Identifier")


class OaDetails(models.Model):
    id = models.UUIDField(default=uuid.uuid4, primary_key=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_for = models.ForeignKey(
        "users.Organisation",
        models.CASCADE,
        blank=True,
        null=True,
    )
    # these fields are blank for incoming documents when we don't know these values yet
    uri = models.CharField(max_length=3000, blank=True)
    key = models.CharField(max_length=3000, blank=True)

    iv_base64 = models.TextField(blank=True)
    tag_base64 = models.TextField(blank=True)

    # after we have it wrapper and issued we store the cyphertext here
    # so it can be returned per request
    # please note the ciphertext contains binary attachments base64 representations,
    # which may be quite space-consuming and generally should'nt be storted in
    # database
    # TODO: fix it and store it as file somewhere
    # Looks like it's not used anymore so much
    ciphertext = models.TextField(blank=True)

    oa_file = models.FileField(blank=True, help_text="Wrapped OA document, JSON")

    class Meta:
        ordering = ("-created_at",)
        verbose_name = _("OA details")
        verbose_name_plural = _("OA details")

    def __str__(self):
        return self.uri

    def url_repr(self):
        # Historical QR codes which are not supported even by tradetrust.io anymore
        # tradetrust://
        # {
        #     "uri":"https://salty-wildwood-95924.herokuapp.com/abc123
        #            #
        #            b3d1961f047eba5eb5ff5582ed3b7fea408bed8860b63bf80c7028c2d8ab356e"
        # }
        # New approach, self-made url
        # (but if we want to use dev.tradetrust.io it must be openattestation)

        # our custom hosts - tradetrust.io won't verify it
        params = {
            "type": "DOCUMENT",
            "payload": {
                "uri": self.uri,
                "key": self.key,
            },
        }
        return f"{settings.UA_BASE_HOST}?q={quote(json.dumps(params))}"

        # marrying tradetrust.io
        # params = {
        #     "type": "DOCUMENT",
        #     "payload": {
        #         "uri": self.uri,
        #         "key": self.key,
        #         # "permittedActions": ["VIEW"],
        #         "redirect": settings.TRADETRUST_VERIFY_HOST
        #     },
        # }
        # return f"https://action.openattestation.com/?q={quote(json.dumps(params))}"

    @classmethod
    def retrieve_new(cls, for_org):
        new_uuid = uuid.uuid4()
        obj = cls.objects.create(
            id=new_uuid,
            created_for=for_org,
            uri=f"{settings.BASE_URL}/oa/{str(new_uuid)}/",
            key=cls._generate_aes_key(),
        )
        return obj

    def get_qr_image(self):
        return get_qrcode_image(self.url_repr())

    def get_qr_image_base64(self):
        return b64encode(self.get_qr_image()).decode("utf-8")

    @classmethod
    def _generate_aes_key(cls, key_len=256):
        return os.urandom(key_len // 8).hex().upper()

    def get_OA_file(self):
        """
        Tries to retrieve a wrapped saved OA document from the history tokens
        May be worth saving it as a file field of the current model itself
        """
        if self.oa_file:
            return self.oa_file

        doc = self.document_set.all().first()
        if not doc:
            return None

        if not doc.is_incoming:
            history_item = (
                doc.history.exclude(related_file="")
                .filter(message__startswith="OA document has been wrapped")
                .first()
            )
            if history_item:
                # outgoing document
                self.oa_file = history_item.related_file
                self.save()
                return history_item.related_file
            return None
        else:
            if doc.intergov_details.get("obj"):
                incoming_obj = doc.files.filter(
                    filename=doc.intergov_details["obj"]
                ).first()
                if incoming_obj:
                    return incoming_obj.file
        return


class Document(models.Model):
    STATUS_NOT_SENT = "not-sent"
    STATUS_PENDING = "pending"
    STATUS_FAILED = "failed"
    STATUS_VALIDATED = "validated"
    STATUS_INCOMING = "incoming"

    MESSAGE_STATUS_CHOICES = (
        (STATUS_NOT_SENT, _("Not Sent")),  # just notarisation
        (STATUS_PENDING, _("Pending")),
        (STATUS_FAILED, _("Failed")),
        (STATUS_VALIDATED, _("Validated")),
        (STATUS_INCOMING, _("Incoming")),
    )

    # OA verification status section
    V_STATUS_NOT_STARTED = "not-started"
    V_STATUS_PENDING = "pending"
    V_STATUS_VALID = "valid"
    V_STATUS_FAILED = "failed"
    V_STATUS_ERROR = "error"

    V_STATUS_CHOICES = (
        (V_STATUS_NOT_STARTED, _("Not Started")),
        (V_STATUS_PENDING, _("Pending")),
        (V_STATUS_VALID, _("Valid")),
        (V_STATUS_FAILED, _("Failed")),
        (V_STATUS_ERROR, _("Error")),
    )

    # UI workflow section - it's responsibility is to show/hide buttons
    # for users wanting to perform some actions
    # https://edi3.org/specs/edi3-regulatory/develop/certificates/#state-lifecycle
    WORKFLOW_STATUS_DRAFT = "draft"
    WORKFLOW_STATUS_ISSUED = "issued"
    WORKFLOW_STATUS_NOT_ISSUED = "not-issued"
    WORKFLOW_STATUS_INCOMING = "incoming"

    WORKFLOW_STATUS_CHOICES = (
        (WORKFLOW_STATUS_DRAFT, "Draft"),
        (WORKFLOW_STATUS_ISSUED, "Issued"),
        (WORKFLOW_STATUS_NOT_ISSUED, "Not issued"),
        (WORKFLOW_STATUS_INCOMING, "Incoming"),
    )

    TYPE_PREF_COO = "pref_coo"
    TYPE_NONPREF_COO = "non_pref_coo"

    TYPE_CHOICES = (
        (TYPE_PREF_COO, _("Preferential Certificate of Origin")),
        (TYPE_NONPREF_COO, _("Non-preferential Certificate of Origin")),
    )

    id = models.UUIDField(default=uuid.uuid4, primary_key=True)

    oa = models.ForeignKey(OaDetails, models.CASCADE, null=True)
    created_at = models.DateTimeField(
        default=timezone.now, verbose_name=_("created at")
    )
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.SET_NULL,
        blank=True,
        null=True,
        verbose_name=_("created by user"),
    )
    created_by_org = models.ForeignKey(
        "users.Organisation",
        models.SET_NULL,
        blank=True,
        null=True,
        verbose_name=_("created by org"),
    )

    type = models.CharField(_("Document type"), max_length=64, choices=TYPE_CHOICES)
    document_number = models.CharField(
        _("Document number"), max_length=256, blank=False, default=""
    )

    fta = models.ForeignKey(
        FTA, models.PROTECT, verbose_name=_("FTA"), blank=True, null=True
    )

    sending_jurisdiction = CountryField(
        default=settings.ICL_APP_COUNTRY, verbose_name=_("Sending jurisdiction")
    )
    importing_country = CountryField(verbose_name=_("Importing jurisdiction"))
    issuer = models.ForeignKey(
        Party,
        models.CASCADE,
        blank=True,
        null=True,
        related_name="documents_issued",
        verbose_name=_("issuer"),
    )
    exporter = models.ForeignKey(
        Party,
        models.CASCADE,
        blank=True,
        null=True,
        related_name="documents_exported",
        verbose_name=_("exporter"),
    )
    importer_name = models.CharField(
        _("Importer name"),
        max_length=256,
        help_text=_("Organisation name or business ID (ABN, UEN)"),
        blank=True,
        default="",
    )

    consignment_ref_doc_number = models.CharField(
        _("Consignment Number"), max_length=256, blank=True, default=""
    )

    intergov_details = JSONField(
        default=dict,
        blank=True,
        help_text=_("Details about communication with the Intergov"),
    )
    # https://edi3.org/specs/edi3-regulatory/develop/certificates/#state-lifecycle
    # IGL message status
    status = models.CharField(
        _("Message Status"),
        max_length=12,
        choices=MESSAGE_STATUS_CHOICES,
        default=STATUS_NOT_SENT,
    )
    verification_status = models.CharField(
        _("Verification Status"),
        max_length=32,
        default=V_STATUS_NOT_STARTED,
        choices=V_STATUS_CHOICES,
    )
    workflow_status = models.CharField(
        _("Workflow status"),
        max_length=32,
        default=WORKFLOW_STATUS_DRAFT,
        choices=WORKFLOW_STATUS_CHOICES,
    )

    extra_data = JSONField(
        default=dict, blank=True, help_text=_("Extra data like the QR code position")
    )

    raw_certificate_data = JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "The data according the UNCoOSpec, most of it we don't parse; "
            "usually created through the API"
        ),
    )

    search_field = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-created_at",)

    def get_absolute_url(self):
        return reverse("documents:detail", args=[self.pk])

    def __str__(self):
        return f"{self.get_type_display()} #{self.short_id}"

    @statsd_timer("model.Document.save")
    def save(self, *args, **kwargs):
        self._fill_search_field()
        super().save(*args, **kwargs)

    def _fill_search_field(self):
        data = [
            self.get_type_display(),
            self.get_status_display(),
            str(self.pk),
            str(self.document_number),
            self.consignment_ref_doc_number,
            self.importing_country.name,
            str(self.fta),
            str(self.exporter),
        ]
        self.search_field = "\n".join(data)
        return

    @property
    def short_id(self):
        return str(self.id)[-6:]

    @property
    def is_api_created(self):
        return bool(self.raw_certificate_data.get("certificateOfOrigin"))

    def get_rendered_edi3_document(self):
        from trade_portal.edi3.certificates import Un20200831CoORenderer

        if self.raw_certificate_data.get("certificateOfOrigin"):
            # has been already rendered or sent through the API
            data = {
                "certificateOfOrigin": self.raw_certificate_data["certificateOfOrigin"]
            }
            # update it by attached files
            pdf_attach = self.get_pdf_attachment()
            if pdf_attach:
                try:
                    data["certificateOfOrigin"]["attachedFile"] = {
                        "file": b64encode(pdf_attach.file.read()).decode("utf-8"),
                        "encodingCode": "base64",
                        "mimeCode": pdf_attach.mimetype(),
                    }
                except Exception as e:
                    logger.exception(e)
                    pass
            return data

        if self.importing_country == settings.ICL_APP_COUNTRY:
            # inbound, the certificate may be somewhere
            if "oa_doc" in self.intergov_details:
                return self.intergov_details["oa_doc"]
            else:
                the_first_file = DocumentFile.objects.filter(
                    doc=self, filename=self.intergov_details.get("obj")
                ).first()
                if the_first_file:
                    return the_first_file.file.read().decode("utf-8")

        if self.is_incoming:
            # This is an incoming document, but it hasn't been downloaded yet
            return None
        else:
            # if came here - then just render it as usual
            return Un20200831CoORenderer().render(self)

    @property
    def is_incoming(self):
        return self.sending_jurisdiction != settings.ICL_APP_COUNTRY

    def get_vc(self):
        return self.oa.get_OA_file()

    def get_pdf_attachment(self):
        return self.files.filter(filename__endswith=".pdf").first()


class DocumentHistoryItem(models.Model):
    document = models.ForeignKey(Document, models.CASCADE, related_name="history")
    created_at = models.DateTimeField(default=timezone.now)
    type = models.CharField(max_length=200)
    message = models.TextField(max_length=2000, blank=True)
    object_body = models.TextField(blank=True)
    linked_obj_id = models.CharField(max_length=128, blank=True)

    # sometimes we want to save large file (like OA unwrapped one)
    related_file = models.FileField(blank=True)

    is_error = models.BooleanField(default=False)

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return self.message

    @property
    def related_object(self):
        if self.type == "nodemessage":
            try:
                return NodeMessage.objects.get(
                    document=self.document, pk=self.linked_obj_id
                )
            except Exception:
                return _("(wrong ref)")
        return None


def generate_docfile_filename(instance, filename):
    """
    We completely ignore the original filename for security reasons,
    but we generate our own filename respecting only the original extension if
    provided.
    """
    upload_ref = (str(instance.pk) if instance.pk else None) or "ref" + str(
        uuid.uuid4()
    )
    upload_id = "".join(random.choice(string.ascii_lowercase) for i in range(7))
    if filename and "." in filename:
        ext = filename.split(".")[-1]
    else:
        # no filename provided - what do we assume?
        ext = "pdf"

    return "docfiles/{}/{}.{}".format(upload_ref, upload_id, ext.lower())


class DocumentFile(models.Model):
    id = models.UUIDField(default=uuid.uuid4, primary_key=True)
    doc = models.ForeignKey(Document, models.CASCADE, related_name="files")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        models.CASCADE,
        blank=True,
        null=True,
    )

    file = models.FileField(
        upload_to=generate_docfile_filename,
    )
    original_file = models.FileField(upload_to=generate_docfile_filename)
    filename = models.CharField(
        max_length=1000,
        blank=True,
        default="unknown.pdf",
        help_text=_("Original name of the uploaded file"),
    )
    size = models.IntegerField(blank=True, default=None, null=True)

    is_watermarked = models.NullBooleanField(
        default=False,
        help_text=_(
            "None if doesn't need to be watermarked, False if pending and True if done"
        ),
    )

    metadata = JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return str(self.filename)

    def save(self, *args, **kwargs):
        if not self.original_file:
            self.original_file = self.file
        super().save(*args, **kwargs)

    @property
    def short_filename(self):
        if len(self.filename) > 25:
            if "." in self.filename:
                return self.filename[:15] + "..." + self.filename[-10:]
            return self.filename[:22] + "..."
        return self.filename

    def extension(self):
        fname = self.filename or ""
        if "." in fname:
            return fname.rsplit(".", maxsplit=1)[1].upper()
        else:
            return "?"

    def mimetype(self):
        return mimetypes.guess_type(self.filename, strict=False)[0]

    def get_size_display(self):
        def sizeof_fmt(num, suffix="B"):
            for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
                if abs(num) < 1024.0:
                    return "%3.1f%s%s" % (num, unit, suffix)
                num /= 1024.0
            return "%.1f%s%s" % (num, "Yi", suffix)

        return sizeof_fmt(self.size) if self.size else ""


class NodeMessage(models.Model):
    STATUS_SENT = "sent"
    STATUS_REJECTED = "rejected"
    STATUS_ACCEPTED = "accepted"
    STATUS_INBOUND = "inbound"

    STATUSES = (
        (STATUS_SENT, _("Sent")),
        (STATUS_ACCEPTED, _("Accepted")),
        (STATUS_REJECTED, _("Rejected")),
        (STATUS_INBOUND, _("Inbound")),
    )

    status = models.CharField(
        max_length=16,
        default=STATUS_SENT,
        choices=STATUSES,
    )

    document = models.ForeignKey(Document, models.CASCADE, blank=True, null=True)
    created_at = models.DateTimeField(
        _("Created in the system"),
        default=timezone.now,
        help_text=_("For received messages - when received by us"),
    )

    sender_ref = models.CharField(max_length=200, unique=True)
    subject = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=_("Conversation identificator"),
    )
    body = JSONField(
        default=dict,
        blank=True,
        help_text=_("generic discrete format, exactly like API returns"),
    )
    history = JSONField(
        default=list,
        blank=True,
        help_text=_("Status changes and other historical events in a simple format"),
    )
    is_outbound = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.sender_ref or self.pk

    def trigger_processing(self, new_status=None):
        """
        We don't update business status based on the status changes from nodes,
        because it needs a business message, not just transport state change
        (apart of errors)

        but we handle business messages from other nodes here
        """
        if self.is_outbound:
            # change status to error if any outboud message is rejected
            if new_status == "rejected":
                self.document.status = Document.STATUS_FAILED
                self.document.save()
                logger.warning(
                    "Change document %s status to Failed due to rejected message %s",
                    self.document,
                    self,
                )
                DocumentHistoryItem.objects.create(
                    is_error=True,
                    type="nodemessage",
                    document=self.document,
                    message=_(
                        "The document marked as Failed because the outbound message has been rejected"
                    ),
                    linked_obj_id=self.id,
                )
            elif new_status == "accepted":
                self.document.status = Document.STATUS_VALIDATED
                self.document.save()
        return
