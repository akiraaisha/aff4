# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.

"""This module implements the standard AFF4 Image."""
import snappy
import struct
import zlib

from pyaff4 import aff4
from pyaff4 import lexicon
from pyaff4 import rdfvalue
from pyaff4 import registry


class AFF4Image(aff4.AFF4Stream):

    @staticmethod
    def NewAFF4Image(resolver, image_urn, volume_urn):
        with resolver.AFF4FactoryOpen(volume_urn) as volume:
            # Inform the volume that we have a new image stream contained within
            # it.
            volume.children.add(image_urn)

            resolver.Set(image_urn, lexicon.AFF4_TYPE, rdfvalue.URN(
                lexicon.AFF4_IMAGE_TYPE))

            resolver.Set(image_urn, lexicon.AFF4_STORED,
                         rdfvalue.URN(volume_urn))

            return resolver.AFF4FactoryOpen(image_urn)

    def LoadFromURN(self):
        volume_urn = self.resolver.Get(self.urn, lexicon.AFF4_STORED)
        if not volume_urn:
            raise IOError("Unable to find storage for urn %s" % self.urn)

        self.chunk_size = int(self.resolver.Get(
            self.urn, lexicon.AFF4_IMAGE_CHUNK_SIZE) or 32*1024)

        self.chunks_per_segment = int(self.resolver.Get(
            self.urn, lexicon.AFF4_IMAGE_CHUNKS_PER_SEGMENT) or 1024)

        self.size = int(
            self.resolver.Get(self.urn, lexicon.AFF4_STREAM_SIZE) or 0)

        self.compression = str(self.resolver.Get(
            self.urn, lexicon.AFF4_IMAGE_COMPRESSION) or
                               lexicon.AFF4_IMAGE_COMPRESSION_ZLIB)

        self.buffer = ""
        self.bevy = ""
        self.bevy_index = ""
        self.chunk_count_in_bevy = 0
        self.bevy_number = 0

    def Write(self, data):
        self.MarkDirty()
        self.buffer += data

        while len(self.buffer) > self.chunk_size:
            chunk = self.buffer[:self.chunk_size]
            self.buffer = self.buffer[self.chunk_size:]
            self.FlushChunk(chunk)

        self.readptr += len(data)
        if self.readptr > self.size:
            self.size = self.readptr

        return len(data)

    def FlushChunk(self, chunk):
        bevy_offset = len(self.bevy)
        if self.compression == lexicon.AFF4_IMAGE_COMPRESSION_ZLIB:
            compressed_chunk = zlib.compress(chunk)
        elif self.compression == lexicon.AFF4_IMAGE_COMPRESSION_SNAPPY:
            compressed_chunk = snappy.compress(chunk)
        elif self.compression == lexicon.AFF4_IMAGE_COMPRESSION_STORED:
            compressed_chunk = chunk

        self.bevy_index += struct.pack("<I", bevy_offset)
        self.bevy += compressed_chunk
        self.chunk_count_in_bevy += 1

        if self.chunk_count_in_bevy >= self.chunks_per_segment:
            self._FlushBevy()

    def _FlushBevy(self):
        volume_urn = self.resolver.Get(self.urn, lexicon.AFF4_STORED)
        if not volume_urn:
            raise IOError("Unable to find storage for urn %s" % self.urn)

        # Bevy is empty nothing to do.
        if not self.bevy:
            return

        bevy_urn = self.urn.Append("%08d" % self.bevy_number)
        bevy_index_urn = bevy_urn.Append("index")

        with self.resolver.AFF4FactoryOpen(volume_urn) as volume:
            with volume.CreateMember(bevy_index_urn) as bevy_index:
                bevy_index.Write(self.bevy_index)

            with volume.CreateMember(bevy_urn) as bevy:
                bevy.Write(self.bevy)

            # We dont need to hold these in memory any more.
            self.resolver.Close(bevy_index)
            self.resolver.Close(bevy)

        self.chunk_count_in_bevy = 0
        self.bevy_number += 1
        self.bevy = ""
        self.bevy_index = ""

    def Flush(self):
        if self.IsDirty():
            # Flush the last chunk.
            self.FlushChunk(self.buffer)
            self._FlushBevy()

            self.resolver.Set(self.urn, lexicon.AFF4_TYPE,
                              rdfvalue.URN(lexicon.AFF4_IMAGE_TYPE))

            self.resolver.Set(self.urn, lexicon.AFF4_IMAGE_CHUNK_SIZE,
                              rdfvalue.XSDInteger(self.chunk_size))

            self.resolver.Set(self.urn, lexicon.AFF4_IMAGE_CHUNKS_PER_SEGMENT,
                              rdfvalue.XSDInteger(self.chunks_per_segment))

            self.resolver.Set(self.urn, lexicon.AFF4_STREAM_SIZE,
                              rdfvalue.XSDInteger(self.Size()))

            self.resolver.Set(
                self.urn, lexicon.AFF4_IMAGE_COMPRESSION,
                rdfvalue.URN(self.compression))

        return super(AFF4Image, self).Flush()

    def Read(self, length):
        length = min(length, self.Size() - self.readptr)

        initial_chunk_offset = self.readptr % self.chunk_size
        # We read this many full chunks at once.
        chunks_to_read = length / self.chunk_size + 1
        chunk_id = self.readptr / self.chunk_size
        result = ""

        while chunks_to_read > 0:
            chunks_read, data = self._ReadPartial(chunk_id, chunks_to_read)
            if chunks_read == 0:
                break

            chunks_to_read -= chunks_read
            result += data

        if initial_chunk_offset:
            result = result[initial_chunk_offset:]

        result = result[:length]

        return result

    def _ReadPartial(self, chunk_id, chunks_to_read):
        chunks_read = 0
        result = ""

        while chunks_to_read > 0:
            bevy_id = chunk_id / self.chunks_per_segment
            bevy_urn = self.urn.Append("%08d" % bevy_id)
            bevy_index_urn = bevy_urn.Append("index")

            with self.resolver.AFF4FactoryOpen(bevy_index_urn) as bevy_index:
                index_size = bevy_index.Size() / 4
                bevy_index_data = bevy_index.Read(bevy_index.Size())

                bevy_index_array = struct.unpack(
                    "<" + "I" * index_size, bevy_index_data)

            with self.resolver.AFF4FactoryOpen(bevy_urn) as bevy:
                while chunks_to_read > 0:
                    # Read a full chunk from the bevy.
                    data = self._ReadChunkFromBevy(
                        chunk_id, bevy, bevy_index_array, index_size)

                    result += data

                    chunks_to_read -= 1
                    chunk_id += 1
                    chunks_read += 1

                    # This bevy is exhausted, get the next one.
                    if bevy_id < chunk_id / self.chunks_per_segment:
                        break

        return chunks_read, result

    def _ReadChunkFromBevy(self, chunk_id, bevy, bevy_index, index_size):
        chunk_id_in_bevy = chunk_id % self.chunks_per_segment

        if index_size == 0:
            raise IOError("Index empty in %s: %s" % (self.urn, chunk_id))
        # The segment is not completely full.
        if chunk_id_in_bevy >= index_size:
            raise IOError("Bevy index too short in %s: %s" % (
                self.urn, chunk_id))

        # For the last chunk in the bevy, consume to the end of the bevy
        # segment.
        if chunk_id_in_bevy == index_size - 1:
            compressed_chunk_size = bevy.Size() - bevy.Tell()
        else:
            compressed_chunk_size = (bevy_index[chunk_id_in_bevy + 1] -
                                     bevy_index[chunk_id_in_bevy])

        bevy.Seek(bevy_index[chunk_id_in_bevy], 0)
        cbuffer = bevy.Read(compressed_chunk_size)
        if self.compression == lexicon.AFF4_IMAGE_COMPRESSION_ZLIB:
            return zlib.decompress(cbuffer)

        if self.compression == lexicon.AFF4_IMAGE_COMPRESSION_SNAPPY:
            return snappy.decompress(cbuffer)

        if self.compression == lexicon.AFF4_IMAGE_COMPRESSION_STORED:
            return cbuffer


registry.AFF4_TYPE_MAP[lexicon.AFF4_IMAGE_TYPE] = AFF4Image
