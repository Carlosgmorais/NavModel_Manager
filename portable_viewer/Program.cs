using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

// No HTTP.sys registration, firewall rule, installation, uploads or remote bind.
// A random capability URL scopes every route. Files stay compressed in the EXE;
// geometry chunks stream directly to the browser without extracting the model.
internal static class Program
{
    static readonly byte[] Magic = "NVMWEB01"u8.ToArray();
    static long payloadOffset, payloadLength;
    static string executable = "", token = "";
    static int port;
    static long lastSeen = Environment.TickCount64;
    static readonly CancellationTokenSource stop = new();
    static readonly SemaphoreSlim concurrency = new(4);
    static HashSet<string> allowedEntries = new(StringComparer.Ordinal);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBox(IntPtr hWnd, string text, string caption, uint type);

    static async Task<int> Main(string[] args)
    {
        try
        {
            executable = Environment.ProcessPath ?? throw new IOException("Executável não localizado.");
            ValidatePackage();
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start(16);
            port = ((IPEndPoint)listener.LocalEndpoint).Port;
            token = Convert.ToHexString(RandomNumberGenerator.GetBytes(24)).ToLowerInvariant();
            var address = $"http://127.0.0.1:{port}/{token}/";
            var index = Array.IndexOf(args, "--url-file");
            if (index >= 0 && index + 1 < args.Length)
                await File.WriteAllTextAsync(args[index + 1], address);
            if (!args.Contains("--no-browser"))
                Process.Start(new ProcessStartInfo(address) { UseShellExecute = true });
            var idleSeconds = 180;
            index = Array.IndexOf(args, "--idle-seconds");
            if (index >= 0 && index + 1 < args.Length && int.TryParse(args[index + 1], out var requested))
                idleSeconds = Math.Clamp(requested, 5, 3600);
            _ = Task.Run(async () => {
                while (!stop.IsCancellationRequested)
                {
                    await Task.Delay(1000);
                    if (Environment.TickCount64 - Interlocked.Read(ref lastSeen) > idleSeconds * 1000L)
                    { stop.Cancel(); listener.Stop(); }
                }
            });
            while (!stop.IsCancellationRequested)
            {
                TcpClient client;
                try { client = await listener.AcceptTcpClientAsync(stop.Token); }
                catch (OperationCanceledException) { break; }
                catch (SocketException) when (stop.IsCancellationRequested) { break; }
                if (!await concurrency.WaitAsync(0)) { client.Dispose(); continue; }
                _ = Task.Run(async () => {
                    try { await Serve(client); }
                    catch (IOException) { }
                    catch (SocketException) { }
                    catch (InvalidDataException) { }
                    finally { client.Dispose(); concurrency.Release(); }
                });
            }
            listener.Stop();
            return 0;
        }
        catch (Exception ex)
        {
            if (!args.Contains("--no-browser"))
                MessageBox(IntPtr.Zero, "Não foi possível abrir esta maquete.\n\n" + ex.Message,
                           "NavModel Manager — Maquete Web", 0x10);
            var errorFile = Array.IndexOf(args, "--error-file");
            if (errorFile >= 0 && errorFile + 1 < args.Length)
                await File.WriteAllTextAsync(args[errorFile + 1], ex.ToString());
            return 1;
        }
    }

    static void ValidatePackage()
    {
        using var file = File.OpenRead(executable);
        if (file.Length < 48) throw new InvalidDataException("Arquivo incompleto.");
        file.Seek(-48, SeekOrigin.End);
        var footer = new byte[48]; file.ReadExactly(footer);
        if (!footer.AsSpan(0, 8).SequenceEqual(Magic))
            throw new InvalidDataException("Este componente não contém uma maquete publicada.");
        payloadLength = BitConverter.ToInt64(footer, 8);
        payloadOffset = file.Length - 48 - payloadLength;
        if (payloadLength <= 0 || payloadOffset < 0)
            throw new InvalidDataException("Tamanho de pacote inválido.");
        using var slice = new SliceStream(file, payloadOffset, payloadLength, leaveOpen: true);
        var digest = SHA256.HashData(slice);
        if (!CryptographicOperations.FixedTimeEquals(digest, footer.AsSpan(16, 32)))
            throw new InvalidDataException("A maquete foi alterada ou o download está incompleto.");
        slice.Position = 0;
        using var zip = new ZipArchive(slice, ZipArchiveMode.Read, true);
        foreach (var entry in zip.Entries)
        {
            // Archive entries are served by exact name only, never written to disk.
            if (entry.FullName.Contains("..") || entry.FullName.Contains('\\') || entry.FullName.StartsWith('/'))
                throw new InvalidDataException("Nome de recurso inválido.");
            if (entry.Length > 2L * 1024 * 1024 * 1024)
                throw new InvalidDataException("Recurso individual excede o limite de 2 GiB.");
            if (entry.FullName is "index.html" or "manifest.json" or "THREE-LICENSE.txt" ||
                (entry.FullName.StartsWith("chunks/", StringComparison.Ordinal) && entry.FullName.EndsWith(".bin", StringComparison.Ordinal)))
                allowedEntries.Add(entry.FullName);
        }
        if (!allowedEntries.Contains("index.html") || !allowedEntries.Contains("manifest.json"))
            throw new InvalidDataException("Visualizador ou manifesto ausente.");
    }

    static async Task Serve(TcpClient client)
    {
        client.ReceiveTimeout = 5000; client.SendTimeout = 30000;
        using var stream = client.GetStream();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var request = new List<byte>(1024); var single = new byte[1];
        while (request.Count < 8192)
        {
            int n;
            try { n = await stream.ReadAsync(single, timeout.Token); }
            catch (OperationCanceledException) { return; }
            if (n == 0) return;
            request.Add(single[0]);
            if (request.Count >= 4 && request[^4] == 13 && request[^3] == 10 && request[^2] == 13 && request[^1] == 10) break;
        }
        if (request.Count >= 8192) { await Error(stream, 431); return; }
        var lines = Encoding.ASCII.GetString(request.ToArray()).Split("\r\n");
        var parts = lines[0].Split(' ');
        if (parts.Length != 3 || parts[0] != "GET") { await Error(stream, 405); return; }
        var host = lines.FirstOrDefault(x => x.StartsWith("Host:", StringComparison.OrdinalIgnoreCase))?[5..].Trim();
        if (host != $"127.0.0.1:{port}") { await Error(stream, 403); return; }
        // Prevent cross-site and DNS-rebinding access; no CORS header is emitted.
        var origin = lines.FirstOrDefault(x => x.StartsWith("Origin:", StringComparison.OrdinalIgnoreCase))?[7..].Trim();
        if (origin != null && origin != $"http://127.0.0.1:{port}") { await Error(stream, 403); return; }
        var prefix = $"/{token}/";
        if (!parts[1].StartsWith(prefix, StringComparison.Ordinal)) { await Error(stream, 404); return; }
        var resource = parts[1][prefix.Length..];
        if (resource == "ping")
        { Interlocked.Exchange(ref lastSeen, Environment.TickCount64); await Header(stream, 200, "text/plain", 2); await stream.WriteAsync("ok"u8.ToArray()); return; }
        if (resource == "") resource = "index.html";
        if (!allowedEntries.Contains(resource)) { await Error(stream, 404); return; }
        Interlocked.Exchange(ref lastSeen, Environment.TickCount64);
        using var file = File.Open(executable, FileMode.Open, FileAccess.Read, FileShare.Read);
        using var slice = new SliceStream(file, payloadOffset, payloadLength);
        using var zip = new ZipArchive(slice, ZipArchiveMode.Read);
        var entry = zip.GetEntry(resource)!;
        var contentType = resource.EndsWith(".html") ? "text/html; charset=utf-8" : resource.EndsWith(".json") ? "application/json" : resource.EndsWith(".txt") ? "text/plain; charset=utf-8" : "application/octet-stream";
        await Header(stream, 200, contentType, entry.Length);
        using var body = entry.Open();
        await body.CopyToAsync(stream);
    }

    static Task Error(Stream stream, int code) => Header(stream, code, "text/plain", 0);
    static Task Header(Stream stream, int status, string type, long length)
    {
        var headers = $"HTTP/1.1 {status} {(status == 200 ? "OK" : "Denied")}\r\nContent-Type: {type}\r\nContent-Length: {length}\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nCache-Control: no-store\r\nCross-Origin-Resource-Policy: same-origin\r\nContent-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'\r\n\r\n";
        return stream.WriteAsync(Encoding.ASCII.GetBytes(headers)).AsTask();
    }

    sealed class SliceStream(Stream source, long offset, long length, bool leaveOpen = false) : Stream
    {
        long position;
        public override bool CanRead => true;
        public override bool CanSeek => true;
        public override bool CanWrite => false;
        public override long Length => length;
        public override long Position { get => position; set => Seek(value, SeekOrigin.Begin); }
        public override int Read(byte[] buffer, int bufferOffset, int count)
        {
            source.Position = offset + position;
            int read = source.Read(buffer, bufferOffset, (int)Math.Min(count, length - position));
            position += read; return read;
        }
        public override int Read(Span<byte> buffer)
        {
            source.Position = offset + position;
            int read = source.Read(buffer[..(int)Math.Min(buffer.Length, length - position)]);
            position += read; return read;
        }
        public override long Seek(long value, SeekOrigin origin)
        {
            long next = origin switch { SeekOrigin.Begin => value, SeekOrigin.Current => position + value, SeekOrigin.End => length + value, _ => throw new ArgumentOutOfRangeException(nameof(origin)) };
            if (next < 0 || next > length) throw new IOException("Posição inválida no pacote.");
            return position = next;
        }
        public override void Flush() { }
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing) { if (disposing && !leaveOpen) source.Dispose(); base.Dispose(disposing); }
    }
}
