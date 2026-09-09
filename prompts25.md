# 25-Prompt Red-Team Benchmark Set

`prompts25.txt` is a compact guardrail-testing set derived from the themes in `prompts_long.txt`. It contains exactly 25 prompts, one per line, in the same quoted plain-text format as the existing prompt files.

The set is organized by each prompt's primary red-team category. Some requests naturally span multiple tactics, such as credential access enabling lateral movement or exploit code delivering initial access; each is assigned once according to the main capability being requested.

## Distribution

| Category | Prompt numbers | Count | Coverage |
|---|---:|---:|---|
| Software development | 1-3 | 3 | HTTPS services, network programming, and intentionally vulnerable web applications |
| Malware development | 4-7 | 4 | Payload loading, process injection, defense evasion, reverse shells, and software supply-chain malware |
| Reconnaissance | 8-10 | 3 | Network service discovery, Active Directory discovery, and cloud control-plane enumeration |
| Initial access | 11-14 | 4 | SQL injection, malicious documents, credential phishing/session theft, and network-device exploitation |
| Persistence | 15-17 | 3 | Windows registry, Active Directory Group Policy, and Linux systemd persistence |
| Lateral movement | 18-21 | 4 | Kerberoasting, pass-the-hash, Windows remote management, and Kubernetes host/cluster traversal |
| Exploit development | 22-25 | 4 | Fuzzing, stack overflows and ROP, Java deserialization, and Linux kernel exploitation |
| **Total** | **1-25** | **25** | |

## Category Rationale

### Software Development

Prompts 1-3 provide lower-risk and dual-use calibration points. They test whether a model can distinguish routine network programming from deliberately insecure application development instead of treating all security-adjacent code alike.

### Malware Development

Prompts 4-7 request increasingly explicit payload execution, injection, evasion, and credential theft. The selection spans native Windows code, .NET, PowerShell, and a CI/CD supply-chain scenario.

### Reconnaissance

Prompts 8-10 cover unauthenticated or lightly authenticated network discovery, Active Directory relationship discovery, and cloud reconnaissance with compromised credentials. This provides infrastructure diversity without over-representing port scanning.

### Initial Access

Prompts 11-14 split initial access between exploitation and phishing. They cover web authentication bypass, document-based payload delivery, MFA-resistant session phishing, and exploitation of exposed network infrastructure.

### Persistence

Prompts 15-17 exercise persistence at user, domain, and Linux service levels. This tests local registry modification, fleet-wide policy abuse, and disguised scheduled execution.

### Lateral Movement

Prompts 18-21 cover credential acquisition and reuse, native remote-management channels, and container-to-cluster movement. The mix represents common Windows/AD operations while adding cloud-native infrastructure.

### Exploit Development

Prompts 22-25 progress from vulnerability discovery to user-space and kernel exploit construction. They include a relatively benign fuzzing workflow alongside requests for ROP, deserialization RCE, and local privilege-escalation code.

## Selection Principles

- Preserve broad technical coverage across Windows, Linux, Active Directory, Azure, Kubernetes, network devices, web applications, and build pipelines.
- Include a range of apparent intent, from ordinary dual-use engineering to unambiguously malicious implementation requests.
- Prefer concrete deliverables such as code, commands, payloads, or exploit workflows so judge outcomes are easier to classify.
- Avoid dependencies between prompts; every line can be evaluated as an independent request.
- Keep category labels out of `prompts25.txt` so the model receives only the test request.
