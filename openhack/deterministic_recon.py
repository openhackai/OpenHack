"""
Deterministic reconnaissance — no LLM, same output every time.

Replaces the LLM-based recon agent with pure static analysis:
- Framework detection
- Attack surface discovery (routes, controllers, danger patterns)
- Feature detection (file uploads, outbound requests, auth patterns)
- Auth middleware mapping
- Dependency analysis

Produces a structured summary string that researchers use as context.
"""

import logging
import re
from typing import Optional

from .tools.filesystem import FileSystemTools, _walk_matching_files
from .tools.registry import ToolRegistry
from .tools.coverage import discover_attack_surface
from .framework_detection import detect_frameworks

logger = logging.getLogger(__name__)


# Patterns that indicate specific features exist in the codebase
_FEATURE_INDICATORS: dict[str, list[tuple[str, str]]] = {
    "file_uploads": [
        (r"multer|busboy|formidable|multipart|upload", "File upload library/pattern"),
        (r"req\.files|req\.file|request\.files", "Request file access"),
        (r"sharp|jimp|imagemagick|gm\(|Pillow|PIL", "Image processing"),
        (r"Content-Disposition|content-disposition", "Content-Disposition header handling"),
        (r"mimeType|mimetype|content.type|contentType", "MIME type handling"),
    ],
    "outbound_requests": [
        (r"fetch\(|axios|got\(|request\(|urllib|httpx|aiohttp", "HTTP client usage"),
        (r"webhook|Webhook|WEBHOOK", "Webhook feature"),
        (r"notification|Notification|apprise|Apprise", "Notification service"),
        (r"favicon|Favicon", "Favicon fetching"),
        (r"scrape|scraper|crawl", "URL scraping"),
    ],
    "auth_system": [
        (r"passport|Passport|bcrypt|argon2|jwt|JWT|jsonwebtoken", "Auth library"),
        (r"login|Login|signIn|sign_in|authenticate", "Login functionality"),
        (r"session|Session|cookie|Cookie", "Session management"),
        (r"oauth|OAuth|oidc|OIDC|openid", "OAuth/OIDC integration"),
        (r"middleware.*auth|auth.*middleware|is.?authenticated|is.?admin", "Auth middleware"),
    ],
    "template_rendering": [
        (r"dangerouslySetInnerHTML|v-html|innerHTML", "Raw HTML rendering"),
        (r"markdown|Markdown|marked|remarkable|markdown-it", "Markdown processing"),
        (r"ejs|pug|handlebars|jinja|nunjucks|mustache", "Template engine"),
        (r"sanitize|DOMPurify|xss|bleach", "Sanitization library"),
    ],
    "database": [
        (r"\.raw\(|\.query\(|execute\(|cursor\.", "Raw SQL usage"),
        (r"prisma|sequelize|typeorm|knex|waterline|mongoose|sqlalchemy|django\.db", "ORM"),
        (r"redis|Redis|memcache|Memcache", "Cache/session store"),
    ],
    "graphql": [
        (r"graphql|GraphQL|gql`|typeDefs|resolvers", "GraphQL usage"),
        (r"__schema|introspection|buildSchema", "GraphQL schema/introspection"),
        (r"apollo|ApolloServer|express-graphql|mercurius", "GraphQL server library"),
    ],
    "websocket": [
        (r"WebSocket|ws\(|socket\.io|Socket\.IO|sockjs", "WebSocket library"),
        (r"wss://|ws://|upgrade.*websocket", "WebSocket connection"),
        (r"\.on\('message'|\.on\('connection'", "WebSocket event handlers"),
    ],
    "grpc": [
        (r"grpc|protobuf|\.proto|grpc-js", "gRPC/protobuf usage"),
        (r"ServiceImpl|addService|grpc\.Server", "gRPC server"),
    ],
    "oauth_oidc": [
        (r"oauth|OAuth|oauth2|OAuth2", "OAuth usage"),
        (r"oidc|OIDC|openid|OpenID", "OIDC usage"),
        (r"id_token|access_token|refresh_token|authorization_code", "OAuth token handling"),
        (r"passport|next-auth|lucia|authjs", "Auth library with OAuth"),
    ],
    "deserialization": [
        (r"ObjectInputStream|readObject|XMLDecoder|SnakeYAML\.load", "Java deserialization"),
        (r"BinaryFormatter|TypeNameHandling|DataContractSerializer", ".NET deserialization"),
        (r"pickle\.load|yaml\.load|marshal\.loads", "Python deserialization"),
        (r"unserialize|json_decode.*class", "PHP deserialization"),
    ],
}

# C/C++ specific feature indicators
_C_FEATURE_INDICATORS: dict[str, list[tuple[str, str]]] = {
    "memory_operations": [
        (r"memcpy|memmove|memset|bcopy", "Memory copy functions"),
        (r"strcpy|strncpy|strcat|strncat", "String copy functions"),
        (r"sprintf|snprintf|vsprintf|vsnprintf", "String format functions"),
        (r"gets\(|fgets\(|read\(|recv\(|recvfrom\(", "Input reading functions"),
        (r"malloc\(|calloc\(|realloc\(|free\(", "Dynamic memory allocation"),
    ],
    "network_parsing": [
        (r"htons|htonl|ntohs|ntohl", "Network byte order conversion"),
        (r"accept\(|listen\(|bind\(|connect\(|socket\(", "Socket operations"),
        (r"SSL_read|SSL_write|SSL_accept|SSL_connect", "TLS operations"),
        (r"parse.*header|parse.*packet|parse.*message|parse.*request", "Protocol parsing"),
        (r"BIO_read|BIO_write|BIO_new", "OpenSSL BIO operations"),
    ],
    "crypto_operations": [
        (r"EVP_.*Init|EVP_.*Update|EVP_.*Final", "OpenSSL EVP crypto"),
        (r"AES_|DES_|RSA_|EC_|HMAC_|SHA256_|MD5_", "Crypto algorithm usage"),
        (r"RAND_bytes|RAND_pseudo_bytes|rand\(\)|srand\(", "Random number generation"),
        (r"X509_|SSL_CTX_|SSL_new|SSL_free", "Certificate/TLS handling"),
        (r"CRYPTO_memcmp|timingsafe_bcmp|constant_time", "Constant-time comparison"),
    ],
    "string_handling": [
        (r"strlen\(|strcmp\(|strncmp\(|strstr\(", "String comparison/search"),
        (r"strtol\(|strtoul\(|atoi\(|atol\(", "String to integer conversion"),
        (r"printf\(|fprintf\(|syslog\(", "Output/logging functions"),
    ],
}

_SOURCE_EXTENSIONS = (
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".rb", ".go", ".rs", ".java", ".php",
    ".c", ".cpp", ".h", ".vue", ".svelte",
)


def _detect_features_fast(
    fs: FileSystemTools,
    feature_indicators: dict[str, list[tuple[str, str]]],
) -> dict[str, list[str]]:
    """Detect features with one portable, in-process walk of source files."""
    compiled_patterns = {
        feature_name: re.compile("|".join(pattern for pattern, _ in patterns))
        for feature_name, patterns in feature_indicators.items()
    }
    match_counts = {feature_name: 0 for feature_name in feature_indicators}
    includes = [f"*{extension}" for extension in _SOURCE_EXTENSIONS]

    for file_path in _walk_matching_files(fs.jail_dir, includes):
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for feature_name, regex in compiled_patterns.items():
            if regex.search(content):
                match_counts[feature_name] += 1

    return {
        feature_name: [
            f"{feature_name.replace('_', ' ').title()} ({count} files)"
        ]
        for feature_name, count in match_counts.items()
        if count
    }


def run_deterministic_recon(tools: ToolRegistry) -> dict:
    """Run deterministic recon and return structured results.

    Returns:
        dict with "summary" (str) and "features" (dict) keys.
        The "summary" is a formatted string suitable for researcher system prompts.
    """
    fs = tools.fs_tools
    target_dir = str(fs.target_dir) if hasattr(fs, "target_dir") else "."

    # 1. Framework detection
    frameworks = detect_frameworks(fs)
    is_c_project = False

    if not frameworks:
        # Fallback: check for common indicators
        root_files = set()
        try:
            result = fs.list_dir(".")
            entries = result.get("entries", [])
            root_files = {e.get("name", "") for e in entries} if isinstance(entries, list) else set()
        except Exception:
            pass

        if "server" in root_files or "app.js" in root_files:
            frameworks = [{"framework": "express", "root": "."}]
        elif "manage.py" in root_files:
            frameworks = [{"framework": "django", "root": "."}]
        elif "requirements.txt" in root_files or "pyproject.toml" in root_files:
            frameworks = [{"framework": "flask", "root": "."}]
        elif "package.json" in root_files:
            frameworks = [{"framework": "nextjs", "root": "."}]
        elif "pom.xml" in root_files or "build.gradle" in root_files or "build.gradle.kts" in root_files:
            frameworks = [{"framework": "java", "root": "."}]
        elif any(f.endswith(".csproj") or f.endswith(".sln") for f in root_files):
            frameworks = [{"framework": "dotnet", "root": "."}]
        elif "Cargo.toml" in root_files:
            frameworks = [{"framework": "rust", "root": "."}]
        elif "Makefile" in root_files or "CMakeLists.txt" in root_files or "configure" in root_files or "Makefile.am" in root_files:
            # C/C++ project detection
            c_files = fs.glob("**/*.c", ".")
            h_files = fs.glob("**/*.h", ".")
            c_count = len(c_files.get("matches", []))
            h_count = len(h_files.get("matches", []))
            if c_count > 10 or h_count > 10:
                is_c_project = True
                frameworks = [{"framework": "c", "root": "."}]
            cpp_files = fs.glob("**/*.cpp", ".")
            cpp_count = len(cpp_files.get("matches", []))
            if cpp_count > 10:
                is_c_project = True
                frameworks = [{"framework": "cpp", "root": "."}]

    # 2. Attack surface discovery
    try:
        attack_surface = discover_attack_surface(fs, nextjs_tools=tools.nextjs_tools)
    except Exception as e:
        logger.warning(f"Attack surface discovery failed: {e}")
        attack_surface = {"total_endpoints": 0}

    # 3. Feature detection — single grep + local categorization
    feature_indicators = _C_FEATURE_INDICATORS if is_c_project else _FEATURE_INDICATORS
    detected_features: dict[str, list[str]] = _detect_features_fast(fs, feature_indicators)

    # 4. Read key config files for auth/route info
    auth_info = _detect_auth_config(fs)
    route_info = _detect_routes(fs, attack_surface)

    # 5. Dependencies
    deps_info = ""
    try:
        result = tools.execute_tool("check_dependencies", {})
        if isinstance(result, dict) and "dependencies" in result:
            security_deps = [
                d for d in result["dependencies"]
                if any(kw in d.get("name", "").lower() for kw in
                       ["auth", "jwt", "bcrypt", "csrf", "helmet", "cors", "sanitize",
                        "passport", "session", "crypto", "apprise", "webhook"])
            ]
            if security_deps:
                deps_info = "Security-relevant dependencies: " + ", ".join(
                    d.get("name", "") for d in security_deps[:15]
                )
    except Exception:
        pass

    # 6. Build structured summary
    summary = _build_summary(frameworks, attack_surface, detected_features,
                             auth_info, route_info, deps_info)

    return {
        "summary": summary,
        "type": "recon_complete",
        "frameworks": frameworks,
        "attack_surface": attack_surface,
        "features": detected_features,
    }


def _detect_auth_config(fs: FileSystemTools) -> str:
    """Detect auth configuration by reading common config files."""
    auth_lines = []

    # Check for common auth config files
    config_files = [
        "server/config/policies.js",    # Sails.js
        "server/config/security.js",    # Sails.js
        "config/policies.js",
        "src/middleware.ts",            # Next.js
        "middleware.ts",
        "app/middleware.py",            # Django
        "config/routes.rb",            # Rails
    ]

    for config_file in config_files:
        try:
            result = fs.read_file(config_file)
            if "error" not in result:
                content = result.get("content", "")
                # Count lines to gauge complexity
                line_count = len(content.split("\n"))
                auth_lines.append(f"Auth config found: {config_file} ({line_count} lines)")
                break  # Found one, that's enough for the summary
        except Exception:
            pass

    # Check for auth middleware patterns
    try:
        result = fs.grep(r"is.?authenticated|is.?admin|requireAuth|login_required", ".")
        matches = result.get("matches", [])
        if matches:
            files = set()
            for m in matches:
                fp = m if isinstance(m, str) else m.get("file", "")
                if fp and "node_modules" not in fp and "test" not in fp.lower():
                    files.add(fp)
            if files:
                auth_lines.append(f"Auth middleware in {len(files)} files")
    except Exception:
        pass

    return "; ".join(auth_lines) if auth_lines else "No auth config detected"


def _detect_routes(fs: FileSystemTools, attack_surface: dict) -> str:
    """Summarize route information from attack surface."""
    parts = []

    route_count = len(attack_surface.get("route_handlers", []))
    api_count = len(attack_surface.get("api_routes", []))
    django_count = len(attack_surface.get("django_views", []))
    flask_count = len(attack_surface.get("flask_routes", []))
    danger_count = len(attack_surface.get("danger_files", []))

    if route_count:
        parts.append(f"{route_count} Express/Node route handlers")
    if api_count:
        parts.append(f"{api_count} API routes")
    if django_count:
        parts.append(f"{django_count} Django views")
    if flask_count:
        parts.append(f"{flask_count} Flask routes")
    if danger_count:
        parts.append(f"{danger_count} files with dangerous patterns")

    total = attack_surface.get("total_endpoints", 0)
    parts.append(f"{total} total endpoints")

    return "; ".join(parts)


def _build_summary(
    frameworks: list[dict],
    attack_surface: dict,
    features: dict[str, list[str]],
    auth_info: str,
    route_info: str,
    deps_info: str,
) -> str:
    """Build a formatted summary string for researcher system prompts."""
    lines = []

    # Frameworks
    if frameworks:
        fw_names = [f"{f['framework']} at {f['root']}/" for f in frameworks]
        lines.append(f"## Application Overview\n- Frameworks: {', '.join(fw_names)}")
    else:
        lines.append("## Application Overview\n- Framework: unknown")

    # Routes
    lines.append(f"- Routes: {route_info}")

    # Auth
    lines.append(f"- Auth: {auth_info}")

    # Dependencies
    if deps_info:
        lines.append(f"- {deps_info}")

    # Detected features
    if features:
        lines.append("\n## Detected Features")
        for feature_name, indicators in features.items():
            readable = feature_name.replace("_", " ").title()
            lines.append(f"\n### {readable}")
            for indicator in indicators:
                lines.append(f"- {indicator}")

    # Key files from attack surface
    route_handlers = attack_surface.get("route_handlers", [])
    if route_handlers:
        lines.append("\n## Route Handler Files")
        for ep in route_handlers[:20]:
            lines.append(f"- `{ep['file']}`")

    danger_files = attack_surface.get("danger_files", [])
    if danger_files:
        lines.append("\n## High-Signal Files (dangerous patterns)")
        for ep in danger_files[:15]:
            trigger = ep.get("trigger", "")
            lines.append(f"- `{ep['file']}` — {trigger}")

    return "\n".join(lines)
