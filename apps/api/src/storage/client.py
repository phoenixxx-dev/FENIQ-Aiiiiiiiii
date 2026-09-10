"""طبقة التخزين — واجهة واحدة، نسختان.

قاعدة ذهبية #3: RAW لا يُمس. الملف الأصلي يُكتب مرة واحدة ولا يُعدَّل ولا يُحذف.

نسختان خلف نفس الواجهة:
  • S3Storage     — MinIO محلياً / S3 إنتاجاً (يُفعَّل عند ضبط S3_ENDPOINT)
  • LocalStorage  — قرص محلي، **للتطوير والاختبار فقط**

⚠️ القرص المحلي غير صالح للإنتاج (يمنع التوسّع الأفقي) — موجود فقط ليشتغل
المشروع على جهاز مطوّر بلا MinIO. اختيار النسخة يتم من الإعدادات لا من الكود.
"""
from __future__ import annotations

import hashlib
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Settings, get_settings


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Storage(ABC):
    @abstractmethod
    def put_file(self, bucket: str, key: str, local_path: str | Path) -> str: ...

    @abstractmethod
    def fetch_to_local(self, bucket: str, key: str, dest_dir: str | Path) -> Path: ...

    @abstractmethod
    def exists(self, bucket: str, key: str) -> bool: ...

    @abstractmethod
    def size(self, bucket: str, key: str) -> int:
        """حجم الكائن بالبايت. يرمي FileNotFoundError إن لم يوجد."""

    @abstractmethod
    def delete(self, bucket: str, key: str) -> None:
        """يحذف كائناً. لا يفشل إن كان غير موجود أصلاً (idempotent).

        ⚠️ لا يستعمله خط المعالجة إطلاقاً — القاعدة الذهبية #3 تمنع المساس
        بالملف الأصلي. هذا المسار للحذف الصريح بطلب صاحب البيانات وحده.
        """

    @abstractmethod
    def health(self) -> bool: ...

    def free_bytes(self) -> int | None:
        """المساحة المتاحة للكتابة، أو None إن كانت غير معروفة.

        مع تخزين سحابي لا سقف عملي فترجع None. مع القرص المحلي — وهو
        الوضع المُعتمد للإنتاج (docs/DECISIONS.md ق-48) — الرقم حقيقي،
        وامتلاء القرص يوقف المنتج كلّه لا الرفع وحده.
        """
        return None

    def delete(self, bucket: str, key: str) -> None:
        self._s3.delete_object(Bucket=bucket, Key=key)

    def presign_put(self, bucket: str, key: str, expires_seconds: int) -> str | None:
        """رابط رفع مباشر موقّع، أو None إن كانت هذه النسخة لا تدعمه.

        الافتراضي None حتى لا تدّعي نسخةٌ قدرةً لا تملكها — المتصل يرتدّ
        عندها للرفع عبر الـAPI.
        """
        return None


class LocalStorage(Storage):
    """تخزين على القرص — تطوير واختبار فقط."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, bucket: str, key: str) -> Path:
        # منع الخروج خارج الجذر عبر مسارات خبيثة (../..)
        p = (self.root / bucket / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("مسار تخزين غير مسموح.")
        return p

    def put_file(self, bucket: str, key: str, local_path: str | Path) -> str:
        dest = self._path(bucket, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        dest.chmod(0o444)          # RAW للقراءة فقط على مستوى نظام الملفات
        return key

    def fetch_to_local(self, bucket: str, key: str, dest_dir: str | Path) -> Path:
        src = self._path(bucket, key)
        if not src.exists():
            raise FileNotFoundError(f"لا يوجد كائن بهذا المفتاح: {bucket}/{key}")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(key).name
        shutil.copy2(src, dest)
        dest.chmod(0o644)
        return dest

    def exists(self, bucket: str, key: str) -> bool:
        return self._path(bucket, key).exists()

    def size(self, bucket: str, key: str) -> int:
        p = self._path(bucket, key)
        if not p.exists():
            raise FileNotFoundError(f"لا يوجد كائن بهذا المفتاح: {bucket}/{key}")
        return p.stat().st_size

    def delete(self, bucket: str, key: str) -> None:
        p = self._path(bucket, key)
        if p.exists():
            # الملف الخام مكتوب بصلاحية قراءة فقط (0444) — نعيد صلاحية
            # الكتابة للمجلد الأب لا للملف: الحذف صلاحية المجلد لا الملف.
            p.chmod(0o644)
            p.unlink()

    def health(self) -> bool:
        return self.root.exists()

    def free_bytes(self) -> int | None:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None


class S3Storage(Storage):
    """MinIO / S3 — نفس الـAPI في التطوير والإنتاج."""

    def __init__(self, settings: Settings):
        import boto3
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )
        for bucket in (settings.s3_bucket_raw, settings.s3_bucket_processed):
            try:
                self._s3.head_bucket(Bucket=bucket)
            except Exception:
                self._s3.create_bucket(Bucket=bucket)

    def put_file(self, bucket: str, key: str, local_path: str | Path) -> str:
        self._s3.upload_file(str(local_path), bucket, key)
        return key

    def fetch_to_local(self, bucket: str, key: str, dest_dir: str | Path) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(key).name
        self._s3.download_file(bucket, key, str(dest))
        return dest

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def size(self, bucket: str, key: str) -> int:
        try:
            return int(self._s3.head_object(Bucket=bucket, Key=key)["ContentLength"])
        except Exception as e:                                    # noqa: BLE001
            raise FileNotFoundError(f"لا يوجد كائن بهذا المفتاح: {bucket}/{key}") from e

    def delete(self, bucket: str, key: str) -> None:
        self._s3.delete_object(Bucket=bucket, Key=key)

    def presign_put(self, bucket: str, key: str, expires_seconds: int) -> str | None:
        """رابط PUT موقّع — الملف يذهب من المتصفح إلى التخزين مباشرة.

        هذا هو المكسب الحقيقي: لا يمر ملف 100 ميغابايت عبر الـAPI إطلاقاً.
        """
        try:
            return self._s3.generate_presigned_url(
                "put_object", Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_seconds)
        except Exception:                                          # noqa: BLE001
            return None

    def health(self) -> bool:
        try:
            self._s3.list_buckets()
            return True
        except Exception:
            return False


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        _storage = S3Storage(s) if s.uses_s3 else LocalStorage(s.local_storage_dir)
    return _storage


def raw_key(user_id: str, dataset_id: str, filename: str) -> str:
    """مفاتيح مقسّمة بالمستخدم — أساس العزل على مستوى التخزين نفسه."""
    safe = Path(filename).name.replace("/", "_").replace("\\", "_")
    return f"{user_id}/{dataset_id}/{safe}"
