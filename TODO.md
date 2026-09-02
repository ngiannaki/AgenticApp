Start as below:
- Create parsers based on langchain. -> did a sample parser. 
- Create a function that checks if a doc needs OCR. -> TODO
- parse pdf to md -> not done
- chunk (overlap, keep tables with up and down text together, create metadata, create a sections table, hierarchical) -> did some work
- index - vector store and pdf save with an ID. -> yes init
- MCP server for parse and index -> OK
- MCP server for retrieval -> OK


cd C:\Users\Nigia\nginx-1.31.3>
.\nginx -s stop

 & "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url https://localhost:8443 --no-tls-verify



 



sections of the doc as reference for the to pic and filter metadata on pages. 


mkcert -key-file localhost-key.pem -cert-file localhost.pem localhost 127.0.0.1 ::1


claude mcp add --transport http indexing https://localhost:8443/indexing/mcp
claude mcp add --transport http retrieval https://localhost:8443/retrieval/mcp
Then verify:


claude mcp list
claude mcp get indexing
claude mcp get retrieval


winget install --id Cloudflare.cloudflared
Run the quick tunnel, pointing at your nginx (skip cert verification between cloudflared and your local self-signed origin)
cloudflared tunnel --url https://localhost:8443 --no-tls-verify
OR
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url https://localhost:8443 --no-tls-verify



It prints a URL like https://random-words-1234.trycloudflare.com. Your connector URLs then become:

https://random-words-1234.trycloudflare.com/indexing/mcp
https://random-words-1234.trycloudflare.com/retrieval/mcp
⚠️ Worth knowing before you do this: your two MCP servers currently have no authentication. A quick-tunnel URL is unguessable but fully public — anyone who obtains it can call upload_document, query_document, etc. with no login. That's fine for a few minutes of testing, but the URL changes every time you restart the tunnel (not stable), and it shouldn't be left running unattended or shared. For anything longer-lived you'd want either a named tunnel tied to your own domain (needs a free Cloudflare account + domain) plus some auth in front of the MCP servers, or to just keep using it locally