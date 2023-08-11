from __future__ import annotations

import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from unstructured.ingest.connector.local import (
    LocalConnector,
    SimpleLocalConfig,
)
from unstructured.ingest.interfaces import (
    BaseConnector,
    BaseConnectorConfig,
    BaseIngestDoc,
    ConnectorCleanupMixin,
    IngestDocCleanupMixin,
    StandardConnectorConfig,
)
from unstructured.ingest.logger import logger

SUPPORTED_REMOTE_FSSPEC_PROTOCOLS = [
    "s3",
    "s3a",
    "abfs",
    "az",
    "gs",
    "gcs",
    "box",
    "dropbox",
]


@dataclass
class SimpleFsspecConfig(BaseConnectorConfig):
    # fsspec specific options
    path: str
    recursive: bool
    access_kwargs: dict = field(default_factory=dict)
    protocol: str = field(init=False)
    path_without_protocol: str = field(init=False)
    dir_path: str = field(init=False)
    file_path: str = field(init=False)
    uncompress: bool = False

    def __post_init__(self):
        self.protocol, self.path_without_protocol = self.path.split("://")
        if self.protocol not in SUPPORTED_REMOTE_FSSPEC_PROTOCOLS:
            raise ValueError(
                f"Protocol {self.protocol} not supported yet, only "
                f"{SUPPORTED_REMOTE_FSSPEC_PROTOCOLS} are supported.",
            )

        # dropbox root is an empty string
        match = re.match(rf"{self.protocol}://([\s])/", self.path)
        if match and self.protocol == "dropbox":
            self.dir_path = " "
            self.file_path = ""
            return

        # just a path with no trailing prefix
        match = re.match(rf"{self.protocol}://([^/\s]+?)(/*)$", self.path)
        if match:
            self.dir_path = match.group(1)
            self.file_path = ""
            return

        # valid path with a dir and/or file
        match = re.match(rf"{self.protocol}://([^/\s]+?)/([^\s]*)", self.path)
        if not match:
            raise ValueError(
                f"Invalid path {self.path}. Expected <protocol>://<dir-path>/<file-or-dir-path>.",
            )
        self.dir_path = match.group(1)
        self.file_path = match.group(2) or ""


@dataclass
class FsspecIngestDoc(IngestDocCleanupMixin, BaseIngestDoc):
    """Class encapsulating fetching a doc and writing processed results (but not
    doing the processing!).

    Also includes a cleanup method. When things go wrong and the cleanup
    method is not called, the file is left behind on the filesystem to assist debugging.
    """

    config: SimpleFsspecConfig
    remote_file_path: str
    is_compressed: bool = False
    children: List["BaseIngestDoc"] = field(default_factory=list)

    def get_children(self) -> List["BaseIngestDoc"]:
        return self.children

    def _tmp_download_file(self):
        return Path(self.standard_config.download_dir) / self.remote_file_path.replace(
            f"{self.config.dir_path}/",
            "",
        )

    def process_file(self, **partition_kwargs) -> Optional[List[Dict[str, Any]]]:
        if self.is_compressed:
            self.config.get_logger().warning(
                f"file detected as zip, skipping process file: {self.filename}",
            )
            return None
        return super().process_file(**partition_kwargs)

    def write_result(self):
        if self.is_compressed:
            self.config.get_logger().warning(
                f"file detected as zip, skipping write results: {self.filename}",
            )
            return None
        return super().write_result()

    @property
    def _output_filename(self):
        return (
            Path(self.standard_config.output_dir)
            / f"{self.remote_file_path.replace(f'{self.config.dir_path}/', '')}.json"
        )

    def _create_full_tmp_dir_path(self):
        """Includes "directories" in the object path"""
        self._tmp_download_file().parent.mkdir(parents=True, exist_ok=True)

    @BaseIngestDoc.skip_if_file_exists
    def get_file(self):
        """Fetches the file from the current filesystem and stores it locally."""
        from fsspec import AbstractFileSystem, get_filesystem_class

        self._create_full_tmp_dir_path()
        fs: AbstractFileSystem = get_filesystem_class(self.config.protocol)(
            **self.config.access_kwargs,
        )
        logger.debug(f"Fetching {self} - PID: {os.getpid()}")
        fs.get(rpath=self.remote_file_path, lpath=self._tmp_download_file().as_posix())
        if zipfile.is_zipfile(self._tmp_download_file().as_posix()):
            self.is_compressed = True
            if self.config.uncompress:
                self.process_zip(zip_path=self._tmp_download_file().as_posix())
        if tarfile.is_tarfile(self._tmp_download_file().as_posix()):
            self.is_compressed = True
            if self.config.uncompress:
                self.process_tar(tar_path=self._tmp_download_file().as_posix())

    def process_zip(self, zip_path: str):
        head, tail = os.path.split(zip_path)
        path = os.path.join(head, f"{tail}-zip-uncompressed")
        self.config.get_logger().info(f"extracting zip {zip_path} -> {path}")
        with zipfile.ZipFile(zip_path) as zfile:
            zfile.extractall(path=path)
        local_connector = LocalConnector(
            standard_config=StandardConnectorConfig(**self.standard_config.__dict__),
            config=SimpleLocalConfig(
                input_path=path,
                recursive=True,
            ),
        )
        self.children.extend(local_connector.get_ingest_docs())

    def process_tar(self, tar_path: str):
        head, tail = os.path.split(tar_path)
        path = os.path.join(head, f"{tail}-tar-uncompressed")
        self.config.get_logger().info(f"extracting tar {tar_path} -> {path}")
        try:
            with tarfile.TarFile(tar_path) as tfile:
                tfile.extractall(path=path)
        except tarfile.ReadError as read_error:
            self.config.get_logger().error(f"failed to uncompress tar {tar_path}: {read_error}")
            return
        local_connector = LocalConnector(
            standard_config=StandardConnectorConfig(**self.standard_config.__dict__),
            config=SimpleLocalConfig(
                input_path=path,
                recursive=True,
            ),
        )
        self.children.extend(local_connector.get_ingest_docs())

    @property
    def filename(self):
        """The filename of the file after downloading from cloud"""
        return self._tmp_download_file()


class FsspecConnector(ConnectorCleanupMixin, BaseConnector):
    """Objects of this class support fetching document(s) from"""

    config: SimpleFsspecConfig
    ingest_doc_cls: Type[FsspecIngestDoc] = FsspecIngestDoc

    def __init__(
        self,
        standard_config: StandardConnectorConfig,
        config: SimpleFsspecConfig,
    ):
        from fsspec import AbstractFileSystem, get_filesystem_class

        super().__init__(standard_config, config)
        self.fs: AbstractFileSystem = get_filesystem_class(self.config.protocol)(
            **self.config.access_kwargs,
        )

    def initialize(self):
        """Verify that can get metadata for an object, validates connections info."""
        ls_output = self.fs.ls(self.config.path_without_protocol)
        if len(ls_output) < 1:
            raise ValueError(
                f"No objects found in {self.config.path}.",
            )

    def _list_files(self):
        if not self.config.recursive:
            # fs.ls does not walk directories
            # directories that are listed in cloud storage can cause problems
            # because they are seen as 0 byte files
            return [
                x.get("name")
                for x in self.fs.ls(self.config.path_without_protocol, detail=True)
                if x.get("size") > 0
            ]
        else:
            # fs.find will recursively walk directories
            # "size" is a common key for all the cloud protocols with fs
            return [
                k
                for k, v in self.fs.find(
                    self.config.path_without_protocol,
                    detail=True,
                ).items()
                if v.get("size") > 0
            ]

    def get_ingest_docs(self):
        return [
            self.ingest_doc_cls(
                standard_config=self.standard_config,
                config=self.config,
                remote_file_path=file,
            )
            for file in self._list_files()
        ]
