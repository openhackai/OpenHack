/** Minimal file handle surface for complete, bounded source reads (short reads are legal). */
export interface SourceReader {
  read(buffer: Buffer, offset: number, length: number, position: number): Promise<{bytesRead: number}>;
}
export async function readSnapshotBytes(reader: SourceReader, expectedSize: number, max: number, signal?: AbortSignal): Promise<Buffer> {
  if (!Number.isSafeInteger(expectedSize) || expectedSize < 0 || expectedSize > max) throw new Error("File too large or not regular");
  const buffer = Buffer.alloc(Math.min(max + 1, expectedSize + 1));
  let length = 0;
  while (length < buffer.length) {
    signal?.throwIfAborted();
    const {bytesRead} = await reader.read(buffer, length, buffer.length - length, length);
    if (!bytesRead) break;
    length += bytesRead;
  }
  signal?.throwIfAborted();
  if (length > max) throw new Error("File exceeds source size limit");
  if (length !== expectedSize) throw new Error("Source changed while being read");
  return buffer.subarray(0, length);
}
