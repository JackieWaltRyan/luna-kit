import contextlib
import hashlib
import io
import logging
import os
import struct
import sys
import warnings
import zlib
from collections import namedtuple
from collections.abc import Callable, Iterable, Iterator
from copy import copy, deepcopy
from ctypes import *
from dataclasses import dataclass
from typing import IO, Annotated, Any, BinaryIO, Literal, NamedTuple

import dataclasses_struct as dcs
import zstandard

from . import enums, types, xxtea
from .file_utils import (PathOrBinaryFile, get_filesize, is_binary_file,
                         is_text_file, open_binary)
from .utils import posix_path, read_ascii_string, trailing_slash


def metadata_by_file_location(metadata: 'FileMetadata'):
    return metadata.file_location

@dcs.dataclass()
class Header():
    file_count: dcs.U32 = 0
    
    metadata_offset: dcs.U32 = 0
    ark_version: dcs.U32 = 0
    # unknown: Annotated[bytes, 20] = b''

HEADER_FORMAT = "3I"


@dcs.dataclass()
class _FileMetadataStruct:
    filename: Annotated[bytes, 128]
    pathname: Annotated[bytes, 128]
    file_location: dcs.U32
    original_filesize: dcs.U32
    compressed_size: dcs.U32
    encrypted_nbytes: dcs.U32
    timestamp: dcs.U32
    md5sum: Annotated[bytes, 16]
    priority: dcs.U32

@dataclass
class FileMetadata:
    filename: str
    pathname: str
    file_location: int
    original_filesize: int
    compressed_size: int
    encrypted_nbytes: int
    timestamp: int
    md5sum: bytes
    priority: int

    @property
    def actual_size(self):
        return self.encrypted_nbytes or self.compressed_size
    
    def __post_init__(self):
        self.__save_original()
        
    def __save_original(self):
        self._filename = self.filename
        self._pathname = self.pathname
        self._file_location = self.file_location
        self._original_filesize = self.original_filesize
        self._compressed_size = self.compressed_size
        self._encrypted_nbytes = self.encrypted_nbytes
        self._timestamp = self.timestamp
        self._md5sum = self.md5sum
        self._priority = self.priority
    
    @property
    def full_path(self):
        return os.path.join(self.pathname, self.filename)
    
    @full_path.setter
    def full_path(self, path: str):
        self.pathname = posix_path(os.path.dirname(path))
        self.filename = posix_path(os.path.basename(path))
    
    def pack(self):
        self.__save_original()
        return _FileMetadataStruct(
            filename = self.filename.encode('ascii', errors = 'ignore'),
            pathname = self.pathname.encode('ascii', errors = 'ignore'),
            file_location = self.file_location,
            original_filesize = self.original_filesize,
            compressed_size = self.compressed_size,
            encrypted_nbytes = self.encrypted_nbytes,
            timestamp = self.timestamp,
            md5sum = self.md5sum,
            priority = self.priority,
        ).pack()


FILE_METADATA_FORMAT = "128s128s5I16sI"

class ARK():
    KEY = [0x3d5b2a34, 0x923fff10, 0x00e346a4, 0x0c74902b]
    
    header: Header
    unknown_header_data: bytes
    _files: 'ARKMetadataCollection[FileMetadata]'
    
    _decompresser = zstandard.ZstdDecompressor()
    
    def __init__(
        self,
        file: str | bytes | bytearray | BinaryIO | None = None,
    ) -> None:
        """Extract `.ark` files.
        
        If you are going to be passing in a file-like object, there are a few things you need to consider.
        
        If you're planning on writing to the file, make sure the file is in read and write mode binary, so `r+b` or `w+b`. This is because I need to read the data in the ark file in order to add to it.

        Args:
            file (str | bytes | bytearray | BinaryIO | None, optional): Input file. Defaults to None.
            output (str | None, optional): Optional output folder to extract files to. Defaults to None.
        """
        self.__open_file: BinaryIO = None
        self.__close_file: bool = False
        self._files = ARKMetadataCollection()
        self.header = Header()
        
        self.file = file
    
    @property
    def files(self):
        return deepcopy(self._files)
    
    def __enter__(self):
        self.load()
        return self
    
    def load(self):
        self.open()
        self.read(self.__open_file)
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def open(self):
        self.close()
        self.__close_file = True
        if isinstance(self.file, str):
            self.__open_file = open(self.file, 'r+b')
        elif isinstance(self.file, (bytes, bytearray)):
            self.__open_file = io.BytesIO(self.file)
        elif is_binary_file(self.file):
            self.__close_file = False
            self.__open_file = self.file
        elif is_text_file(self.file):
            raise TypeError('file must be open in binary mode')
        else:
            raise TypeError('cannot open file')
    
    def close(self):
        logging.debug('closing file')
        if self.__close_file:
            if not self.__open_file.closed:
                self.__open_file.close()
        self.__close_file = False
    
    def __del__(self):
        self.close()
    
    def read(self, file: BinaryIO):
        """Extract `.ark` files.

        Args:
            file (str | bytes | bytearray | BinaryIO | None, optional): Input file.
            output (str | None, optional): Optional output folder to extract files to. Defaults to None.

        Raises:
            TypeError: file must be open in binary mode
            TypeError: cannot open file
        """
        if not is_binary_file(file):
            raise TypeError('file must be file-like object open in binary read mode')
        
        self._files = []
        
        file.seek(0)
        
        self.header = self._read_header(file)
        self._files = self._read_metadata(file)
    

    def write(self, file: BinaryIO):
        if not is_binary_file(file):
            raise TypeError('file must be file-like object open in binary write mode')
        
        file.seek(0)
        packed_files = self._pack_files()
        self.header.metadata_offset = dcs.get_struct_size(Header) + len(self.unknown_header_data)
        for data, meta in packed_files:
            
            
            meta.file_location = self.header.metadata_offset
            self.header.metadata_offset += len(data)
        
        self._write_header(self.__open_file)
        self._write_files_and_metadata(self.__open_file, packed_files)
    
    def read_file(self, file: FileMetadata):
        return self._get_file_data(file, self.__open_file)

    def add_file(self, file: 'ARKFile'):
        data, metadata = file.pack()
        self._write_file(data, metadata, self.__open_file)
    
    def _read_header(self, file: IO) -> Header:
        """Read the header of a `.ark` file.

        Args:
            file (IO): File-like object.

        Returns:
            dict: Header.
        """
        self.unknown_header_data = b''
        
        header: Header = Header.from_packed(
            file.read(dcs.get_struct_size(Header))
        )
        
        if header.ark_version == 3:
            self.unknown_header_data = file.read(20)
        
        return header
    
    
    def _write_header(self, file: IO):
        file.seek(0)
        self._files.sort(key = metadata_by_file_location)
        self.header.file_count = len(self._files)
        
        self.header.metadata_offset = self._files[-1].file_location + (self._files[-1].encrypted_nbytes)
        
        file.write(self.header.pack())
        
        file.write(self.unknown_header_data)


    def _read_metadata(self, file: IO) -> None | list[_FileMetadataStruct]:
        filesize: int = None
        
        file.seek(0, os.SEEK_END)
        
        filesize = file.tell()
        # print(filesize)
        
        if filesize < 0:
            raise TypeError('file size is negative, somehow...')
        
        metadata_size = xxtea.get_phdr_size(filesize - self.header.metadata_offset)
        # print(f'metadata size: {metadata_size}')
        
        raw_metadata_size = self.header.file_count * dcs.get_struct_size(_FileMetadataStruct)
        # print(f'raw metadata size: {raw_metadata_size}')
        
        
        file.seek(self.header.metadata_offset, os.SEEK_SET)
        
        
        metadata = file.read(metadata_size)
        
        # print(f'metadata: {int.from_bytes(metadata, 'little')}')
        metadata = xxtea.decrypt(metadata, metadata_size // 4, self.KEY)
        
        if self.header.ark_version == 1:
            raw_metadata = metadata
        elif self.header.ark_version == 3:
            raw_metadata = self._decompresser.decompress(metadata, raw_metadata_size)
            
            

        metadata_size = dcs.get_struct_size(_FileMetadataStruct)
        result = ARKMetadataCollection()
        for file_index in range(self.header.file_count):
            offset = file_index * metadata_size
            
            
            file_result: _FileMetadataStruct = _FileMetadataStruct.from_packed(
                raw_metadata[offset : offset + metadata_size]
            )
            
            result.append(FileMetadata(
                filename = read_ascii_string(file_result.filename),
                pathname = read_ascii_string(file_result.pathname),
                file_location = file_result.file_location,
                original_filesize = file_result.original_filesize,
                compressed_size = file_result.compressed_size,
                encrypted_nbytes = file_result.encrypted_nbytes,
                timestamp = file_result.timestamp,
                md5sum = bytes.fromhex(file_result.md5sum.hex()),
                priority = file_result.priority,
            ))

        return result
    
    def _fix_metadata(self, metadata: list[FileMetadata]):
        for file in metadata:

            file.filename = file.filename.decode('ascii').rstrip('\x00')
            file.pathname = file.pathname.decode('ascii').rstrip('\x00')
            file.md5sum = file.md5sum.hex()
        
        return metadata
            

    def _get_file_data(self, metadata: FileMetadata, file: BinaryIO):
        file.seek(metadata.file_location, os.SEEK_SET)
        
        file_data = file.read(metadata.encrypted_nbytes if metadata.encrypted_nbytes else metadata.compressed_size)

        compressed = False
        encrypted = False

        if (metadata.encrypted_nbytes) != 0:
            encrypted = True
            file_data = xxtea.decrypt(file_data, metadata.encrypted_nbytes // 4, self.KEY)
        
        if (metadata.compressed_size != metadata.original_filesize):
            compressed = True
            if self.header.ark_version == 1:
                file_data = zlib.decompress(file_data)
            elif self.header.ark_version == 3:
                file_data = self._decompresser.decompress(file_data, metadata.original_filesize)
        
        file_data = file_data[:metadata.original_filesize]
        
        if hashlib.md5(file_data).hexdigest() != metadata.md5sum.hex():
            warnings.warn(f'file "{posix_path(os.path.join(metadata.pathname, metadata.filename))}" hash does not match "{metadata.md5sum.hex()}"')
        
        return ARKFile(
            os.path.join(metadata.pathname, metadata.filename),
            file_data,
            encrypted = encrypted,
            compressed = compressed,
            priority = metadata.priority,
        )
    
    def _write_file(self, data: bytes, metadata: FileMetadata, file: BinaryIO):
        self._files.sort(key = metadata_by_file_location)
        
        if metadata.file_location < 0:
            if len(self._files):
                metadata.file_location = (self._files[-1].file_location + (self._files[-1].encrypted_nbytes or self._files[-1].compressed_size))
            else:
                metadata.file_location = dcs.get_struct_size(self.header) + len(self.unknown_header_data)
        if metadata.full_path not in self._files:
            metadata.file_location = self._files[-1].file_location + (self._files[-1].encrypted_nbytes or self._files[-1].compressed_size)
            self._files.append(metadata)
            file.seek(metadata.file_location)
            file.truncate()
            file.write(data)
            self.header.metadata_offset = metadata.file_location + (metadata.encrypted_nbytes or metadata.compressed_size)
        else:
            found = self._files[metadata.full_path]
            current_index = self._files.index(found)
            rest_start = found.file_location + (found.encrypted_nbytes or found.compressed_size)
            file.seek(rest_start)
            rest = file.read()

            metadata.file_location = found.file_location
            found.compressed_size = metadata.compressed_size
            found.encrypted_nbytes = metadata.encrypted_nbytes
            found.original_filesize = metadata.original_filesize
            found.md5sum = metadata.md5sum
            found.priority = metadata.priority
            found.timestamp = metadata.timestamp
            
            file.seek(metadata.file_location)
            file.truncate()
            file.write(data)
            offset = file.tell() - rest_start
            file.write(rest)

            for i in range(current_index + 1, len(self._files)):
                self._files[i].file_location += offset
            
            self.header.metadata_offset += offset
        
        self._write_header(file)
        self._write_metadata(file)
        

    def _pack_files(self) -> list[tuple[bytes, _FileMetadataStruct]]:
        packed = []
        
        for file in self._files:
            if not isinstance(file, ARKFile):
                raise TypeError('file must be instance of ARKFile')
            
            data, meta = file.pack()
            
            packed.append((data, meta))
        
        return packed
    
    def _write_metadata(self, file: BinaryIO):
        self._files.sort(key = metadata_by_file_location)
        print('filesize', get_filesize(file))
        print('metadata_offset', self.header.metadata_offset)
        file.seek(self.header.metadata_offset)
        print('current pos', file.tell())
        expected_size = self.header.file_count * dcs.get_struct_size(_FileMetadataStruct)
        file.truncate()
        if file.tell() != self.header.metadata_offset:
            self.header.metadata_offset = file.tell()
            self._write_header()
        metadata_block = b''
        for metadata in self._files:
            metadata_block += metadata.pack()
        
        print('expected size:', expected_size)
        print('actual size:', len(metadata_block))
        
        if self.header.ark_version == 1:
            pass
        elif self.header.ark_version == 3:
            metadata_block = zstandard.compress(metadata_block, 9)
        
        print('compressed size', len(metadata_block))
        metadata_block = xxtea.encrypt(metadata_block, self.KEY)
        print('encrypted size', len(metadata_block))

        file.write(metadata_block)

    def _write_files_and_metadata(self, file: IO, packed_files: list[tuple[bytes, _FileMetadataStruct]]):
        metadata_block: bytes = b''
        for data, meta in packed_files:
            file.seek(meta.file_location)
            file.write(data)
            metadata_block += meta.pack()
        
        file.seek(self.header.metadata_offset)
        metadata_block = zstandard.compress(metadata_block)
        
        metadata_block = xxtea.encrypt(metadata_block, self.KEY)
        file.write(metadata_block)
        
class ARKFile():
    def __init__(
        self,
        filename: str,
        data: bytes,
        compressed: bool = True,
        encrypted: bool = False,
        priority: int = 0,
    ) -> None:
        """File inside `.ark` file.

        Args:
            filename (str): The filename to be used inside the `.ark` file.
            data (bytes): File data.
        """
        self.fullpath = str(filename)
        self.data: bytes = bytes(data)
        
        self.compressed = compressed
        self.encrypted = encrypted
        
        self.priority = priority
    
    @property
    def filename(self) -> str:
        """The base filename, such as `bar.txt`.

        Returns:
            str: filename
        """
        return os.path.basename(self.fullpath)
    @filename.setter
    def filename(self, name: str):
        self.fullpath = os.path.join(self.pathname, name)
    
    @property
    def pathname(self) -> str:
        """The directory path of the file, such as `foo/`. Outputs in posix format.

        Returns:
            str: directory
        """
        return trailing_slash(posix_path(os.path.dirname(self.fullpath)))
    @pathname.setter
    def pathname(self, name: str):
        self.fullpath = os.path.join(name, self.filename)
    
    @property
    def fullpath(self) -> str:
        """The full path of the file, such as `foo/bar.txt`. Output is in posix format.

        Returns:
            str: full path
        """
        self.fullpath = self._fullpath
        return self._fullpath

    @fullpath.setter
    def fullpath(self, path: str):
        self._fullpath = posix_path(path)
    
    def save(self, path: str | None = None):
        """Save this file to disk.

        Args:
            path (str | None, optional): Output filepath. Defaults to `fullpath`.
        """
        if path == None:
            path = self.fullpath
        
        os.makedirs(os.path.dirname(path), exist_ok = True)
        
        with open(path, 'wb') as file:
            file.write(self.data)
    
    def pack(self) -> tuple[bytes, FileMetadata]:
        result = self.data
        metadata = FileMetadata(
            filename = self.filename,
            pathname = self.pathname,
            file_location = -1,
            original_filesize = len(result),
            compressed_size = 0,
            encrypted_nbytes = 0,
            timestamp = 0,
            md5sum = bytes.fromhex(hashlib.md5(result).hexdigest()),
            priority = self.priority,
        )
        if self.compressed:
            result = zstandard.compress(result, 9)
            metadata.compressed_size = len(result)

        if self.encrypted:
            result = xxtea.encrypt(result, ARK.KEY)
            metadata.encrypted_nbytes = len(result)
        
        return result, metadata


class ARKMetadataCollection(list):
    def __init__(self, metadatas: Iterable[FileMetadata] | None = None):
        if metadatas is None:
            super().__init__()
        else:
            super().__init__(metadatas)
    
    def get(self, key: str | int, default: Any = None) -> FileMetadata | Any:
        try:
            return self.__getitem__(key)
        except KeyError:
            return default
    
    def setdefault(self, key: str | int, default: FileMetadata) -> FileMetadata:
        try:
            return self.__getitem__(key)
        except KeyError:
            self.__setitem__(key, default)
            return default
    
    def sort(self, *, key: Callable[[FileMetadata], Any] | None = lambda m: m.file_location, reverse: bool = False):
        return super().sort(key = key, reverse = reverse)
        
    def __getitem__(self, key: str | int) -> FileMetadata:
        if isinstance(key, str):
            for index, value in enumerate(self):
                if value.full_path == key:
                    key = index
                    break
        
        return super().__getitem__(key)

    def __setitem__(self, key: str | int, value: FileMetadata):
        if isinstance(key, str):
            for index, value in enumerate(self):
                if value.full_path == key:
                    key = index
                    break
        
        if isinstance(key, str):
            value.full_path = key
            self.append(value)
            return
        
        return super().__setitem__(key, value)
    
    def index(self, value: str | FileMetadata, start: int = 0, stop: int = sys.maxsize):
        if isinstance(value, str):
            for index in range(start, min(stop, len(self))):
                if self[index].full_path == value:
                    return index
        return super().index(value, start, stop)
    
    def copy(self):
        return ARKMetadataCollection(self)
    
    def __contains__(self, value: FileMetadata | str):
        if isinstance(value, str):
            for metadata in self:
                if metadata.full_path == value:
                    return True
        return super().__contains__(value)
    
    def __iter__(self) -> Iterator[FileMetadata]:
        return super().__iter__()
