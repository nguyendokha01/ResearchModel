# Hướng dẫn sử dụng — Kho tri thức y sinh

Phần mềm biến PDF bài báo thành kho bằng chứng có truy vết: câu trích do **code** chép từ nguồn; câu do AI viết được kiểm bằng
quy tắc trích dẫn, số liệu và từ dè dặt. Các quy tắc không bảo đảm mọi diễn giải khoa học đúng; quyết định cuối cùng là của bạn.
Tất cả chạy trên máy này; trang web chỉ mở được từ chính máy (`127.0.0.1`).

Thư mục làm việc: thư mục dự án. Trên máy mới, `1 - Mo trang chinh.bat` tự cài môi trường Python ở lần chạy đầu.
Tài liệu khác: lệnh cho AI ở `docs/AGENT_TOOLS.md`; thiết kế ở `docs/ARCHITECTURE.md`.

## Các file bấm đúp

| File | Dùng khi |
|---|---|
| `1 - Mo trang chinh.bat` | Lần đầu trên máy mới nó tự cài đặt (cần cài trước **Python 3.11+**, có mạng; nên cài Obsidian để đọc và viết ghi chú; phần mềm chạy được khi chưa cài). Mọi lúc: **Thư viện** (nhập và phân tích), **Tìm kiếm**, **Mạng ghi chú** (Neural), **Knowledge graph**. Đóng cửa sổ này là tắt trang |
| `2 - Ket noi AI (MCP).bat` | Nối ứng dụng AI (Claude Desktop, Codex, Gemini CLI, Cursor…) với kho |

## Bắt đầu nhanh (mọi chủ đề)

1. Bấm `1 - Mo trang chinh.bat`, rồi **Thư viện**.
2. **Chủ đề (vault):** tạo vault cho chủ đề (ví dụ `endometriosis`), ghi một câu mô tả; AI dùng câu này để phân loại bài
   (CORE / CONNECTED / POTENTIAL_BRIDGE / UNCERTAIN / OUT_OF_SCOPE). Tên có dấu được đổi thành tên thư mục không dấu.
3. **Thêm nguồn:** kéo thả PDF hoặc ZIP (tự giải nén). File bổ sung XLSX/CSV/PDF có chữ "supp", "S1", "mmc1"… trong tên được gắn
   vào bài chính nếu khớp DOI hoặc tên.
4. Chọn AI (mục *Chọn AI*), rồi chạy ba nút nhanh trong Thư viện, hoặc **bảy bước riêng** trong **Mạng ghi chú**:
   **Nhập nguồn → Kiểm chữ/OCR → Rà cấu trúc → Phân tích AI → Nối kiến thức → Cập nhật vector → Cập nhật notes**.
   Mỗi bước có checkpoint; đóng và tiếp tục sau được; cache chỉ dùng lại khi file đầu vào và đầu ra còn khớp.
   `waiting` nghĩa là còn việc phải làm (ví dụ chờ AI qua MCP), `failed` là có lỗi cần xử lý. Mặc định phần mềm không tự gọi AI ngoài.
5. Dùng: **Tìm kiếm** (hỏi tiếng Việt hoặc Anh), **Mạng ghi chú**, **Knowledge graph** và Obsidian (nếu đã cài).

Thêm bài cho chủ đề cũ: chọn vault đó, thả tệp mới, chạy lại các bước; chỉ bài mới được xử lý. Bài trùng (DOI, PMID, tên + năm,
hoặc cùng file) chỉ lưu một lần và được gắn thêm vào vault mới (không bị đánh cờ). Mỗi chủ đề là một vault riêng.

Trong Thư viện, **Xem cấu trúc** mở phần WH, từ viết tắt và câu Methods của một bài. Nút **So sánh protocol** giữ câu nguồn và thứ tự;
hồ sơ đầy đủ cần AI nộp qua MCP.

## Chọn AI

Hai cách, dùng một hoặc cả hai:

- **Ứng dụng AI qua MCP** (khuyên dùng khi chia sẻ): bấm `2 - Ket noi AI (MCP).bat`, chọn ứng dụng, khởi động lại ứng dụng đó. Ở
  bước phân tích trang Thư viện hiện sẵn câu để nhắn cho ứng dụng AI. Ứng dụng dùng tài khoản của chính bạn; mọi thứ nó nộp vẫn
  qua cùng bộ kiểm tra. Chi tiết: `docs/AI_ANALYSIS_GUIDE.md`.
- **Phần mềm tự gọi AI:** sửa `provider` trong `config/ai.yaml`: `ollama` (miễn phí, chạy trên máy), `anthropic`, `gemini`, `deepseek`,
  `openrouter`, `openai_compatible`, `codex_cli` (Codex đã đăng nhập ChatGPT trên máy), hoặc `command` (lệnh AI bất kỳ, ví dụ
  `["claude", "-p"]`). Key đặt trong `config/secrets.yaml` (ví dụ `gemini_api_key: ...`; file này không bao giờ vào git hay gói chia
  sẻ). Mỗi việc có thể dùng AI riêng trong mục `tasks` (phân tích, trả lời, tổng hợp, bổ sung can thiệp, kiểm chéo V13 — nên dùng
  model khác model đã phân tích). `provider: mcp_agent` = không tự gọi AI nào (mặc định hiện nay; riêng việc trả lời câu hỏi dùng `codex_cli`).
  Sửa xong thì đóng trang và mở lại.

Gửi bài cho AI bên ngoài tốn hạn mức và gửi nội dung bài cho nhà cung cấp: chỉ làm khi bạn đồng ý.

## Kết quả trong Obsidian

Nên cài Obsidian để đọc và viết ghi chú; phần mềm chạy được khi chưa cài (ghi chú vẫn là file Markdown trong `vaults\`).
Mở thư mục `vaults\<tên vault>` bằng Obsidian (**Open folder as vault** lần đầu). Mở thư mục `vaults` để mở **vault mẹ**: Home,
Atlas, danh sách conflict của mọi vault, Registry vault và bài báo. Nếu chưa dùng plugin cộng đồng, bật một lần: **Settings →
Community plugins → Turn on**; Dataview và Extended Graph đã được cấu hình sẵn.

Cấu trúc một vault:
- `Home.md` trang bắt đầu · `_Maps/` Index, Log, Open conflicts, các MOC (bản đồ chủ đề), chú giải màu graph.
- `Papers/` mỗi bài một trang (tóm tắt, bằng chứng theo mục, trích dẫn đi/đến).
- `Substances/` (chất, marker), `Methods/`, `Organisms/` (loài, loại tế bào), `Concepts/`: node do máy tạo từ nguồn.
- `Results/` (EVD: kết quả chính), `Claims/` (CLM: kết luận; CONFLICT: mâu thuẫn), `Review Queue/`.
- `Mine/Ideas`, `Mine/Questions`, `Mine/Hypotheses`, `Mine/Decisions`, `Mine/Knowledge Gaps`: **ghi chú của bạn**; máy không ghi đè hay xóa.
- `_Bases/`, `_Templates/`, `_Attachments/<mã bài>/` (bảng, mẫu, ảnh cắt hình/bảng/công thức).

Phần giữa `kb:auto:start` và `kb:auto:end` do phần mềm cập nhật; bạn viết ở ngoài (ví dụ mục **My notes**). Ghi chú dài chỉ hiện
phần đầu và một link "xem đầy đủ trên dashboard".

**Mâu thuẫn không bao giờ bị xóa.** Ký hiệu: 🔴 mâu thuẫn trong điều kiện so sánh được · 🟠 ngược chiều nhưng chưa rõ so sánh được
· 🟡 bài này có hiệu ứng, bài kia không · ✅ đồng thuận. Nhóm 🔴🟠🟡 có bảng ↑ tăng / ↓ giảm / = không đổi đặt các câu kết quả cạnh nhau;
cặp đối lập nối bằng **⟷**. Ghi chú có xung đột bắt đầu bằng khối đỏ `[!conflict]`; đừng coi đó là kết luận khoa học. Trên graph chỉ
nút Conflict màu đỏ; bài, phương pháp, chất, sinh vật, kết quả giữ màu riêng. Tên chỉ số được coi là đồng nghĩa nằm trong
`knowledge_store\measure_groups.json`: đổi `status` thành `human_rejected` nếu gộp sai.

Kết quả node card và đọc hình/bảng là **đọc máy** do code kiểm: mỗi nhận định chỉ ra mã câu, câu trích do code chép, hedge như
*may*, *suggest* được giữ. Kiểm tra code không có nghĩa là khoa học được chấp nhận.

## Tìm kiếm và hỏi

Trang **Tìm kiếm** có *Google + AI*, *Báo cáo 5 phần*, *Bảng so sánh* (không cần AI), và chế độ *Nhanh* / *Sâu*. Câu trả lời mặc
định có bốn phần:

1. **Bằng chứng từ tài liệu** — chỉ các câu nguyên văn do phần mềm chép từ kho, kèm tác giả, năm, trang và mã câu.
2. **Trả lời dựa trên bằng chứng** — nói lại đúng điều các câu đó cho phép; giữ nguyên may/suggest/associated, số liệu, đơn vị, loài, điều kiện.
3. **[REASONING] Phần suy luận của AI** — tổng hợp hoặc nối nguồn, node, link. Không phải câu trích từ bài, không phải bằng chứng trực tiếp.
4. **Không tìm thấy / không chắc chắn** — phần kho chưa có dữ liệu hoặc tác giả còn dè dặt. "Không thấy trong kho" không có nghĩa là
   "không có trong y văn".

Hãy dùng mã câu ở phần Bằng chứng để mở lại câu nguồn (bấm số trích dẫn để xem mục tham khảo; mở PDF đúng trang). Câu không qua kiểm
tra bị gạch kèm lý do: A1 không dẫn nguồn, A2 số liệu không có trong nguồn, A3 nói chắc hơn nguồn hoặc biến liên quan thành nhân quả,
A4 ý của bài khác viết như kết quả gốc, A5 chỉ dựa vào review (cảnh báo), A6 ngược chiều nguồn, A7 bỏ mất từ dè dặt của nguồn, A8 phần
suy luận dẫn tới ghi chú không có trong lượt tìm.

Bằng chứng do `hermes/kr-auto` tạo (nếu còn) có nhãn **nháp tự động**: kết quả theo luật, chưa phải AI đọc bài. Hãy kiểm tra lại trước khi dùng.

Nút **Dò trùng lặp** nhận một đoạn văn dán vào và tìm các câu trong kho dùng từ giống nhau. Kết quả là **lexical overlap**, không phải
kết luận đạo văn.

Tab **Mạng ghi chú** (Neural) có chế độ *Nhanh* và *Sâu*: đọc ghi chú trong vault hiện tại, đi theo các liên kết thật để tìm ngữ cảnh,
rồi quay về câu nguồn trong kho. *Nhanh* ít ngữ cảnh hơn, không có nghĩa là luôn đúng hơn. Phạm vi mặc định là vault đang chọn.
Bạn có thể xem ghi chú, chuẩn bị prompt cho AI và kiểm JSON bằng chứng theo phiên truy hồi.

Từ làm câu bớt chắc chắn đọc từ `config/hedge_lexicon.yaml`. Thêm không cần lập trình: mở file, thêm một dòng trong `entries`, ví dụ

```yaml
  - {phrase: "apparently", category: appearance, strength: moderate, example: "The change was apparently reversible."}
```

Dùng `phrase` cho từ/cụm cố định; chỉ dùng `regex` khi thật cần. Đừng đổi tên `category` nếu chưa hiểu quy tắc. Từ đồng nghĩa để mở rộng tìm
kiếm nằm ở `config/synonyms.yaml` (chỉ thêm khi chắc chắn tương đương).

## Lệnh hay dùng (cho người quen dòng lệnh)

Trong thư mục dự án: `.venv\Scripts\python.exe scripts\kb.py <lệnh>`; danh sách đầy đủ `kb.py -h` và `docs/AGENT_TOOLS.md`.
Làm mới ghi chú một vault (không gọi AI): `notes --vault V`. Nhập/phân tích một bài thử (tốn hạn mức, hỏi trước): `ingest --vault V`,
`analyze --vault V --limit 1`; chi phí đã ghi: `analysis-usage <mã bài>`. AI đọc hình/bảng: `figure-reading run-pending --vault V`;
AI điền node card: `node-card run-pending --vault V --limit N`. Nhập Word/ghi chú/ảnh/transcript: `source import FILE --vault V`.
Vault mẹ: `mother-vault`; plugin Obsidian đã ghim phiên bản: `obsidian-plugins install`. Chuyển vault cũ sang layout mới:
`vault-migrate --vault V --dry-run` rồi bỏ `--dry-run` (hoàn tác `--undo MAP`). Zotero/Mendeley/EndNote: `import-refs <file>
--dry-run` rồi bỏ `--dry-run`. Kiểm kho: `selftest`.

## Việc khác

- **PDF scan:** trang không có chữ được đọc bằng OCR (RapidOCR, trên máy, ~30 giây/trang). Câu OCR được gắn nhãn và bài có mục `ocr_used`
  trong Review Queue; đối chiếu số liệu với PDF. Chỉnh trong `config/ingest.yaml`, mục `ocr`.
- **Xóa bài:** trong **Thư viện**, mỗi bài có nút **Xóa bài**. Bấm nút, đọc hộp xác nhận, rồi đồng ý. Bài được chuyển vào
  `knowledge_store\removed\deleted_<mã bài>_<thời gian>\` (có `manifest.json`) nên lấy lại được. Bài bị gỡ khỏi mọi vault chứa nó.
  Đang có việc chạy trong Thư viện thì nút từ chối; chờ xong rồi bấm lại.
- **Lưu trữ PDF sang ổ khác / khôi phục:** dùng dòng lệnh `kb.py archive-source` và `kb.py restore-source` (kiểm SHA-256 trước và sau khi chép).
- **PDF không có chữ, bảng khó, công thức:** crop ảnh được giữ nguyên; bảng "uncertain" chỉ hiện ô thô để đối chiếu nguồn; số liệu chỉ có
  trong hình không bao giờ được đọc thành số.

## Phân tích AI thật cho một bài

Có thể tốn hạn mức và gửi nội dung bài ra ngoài: chỉ chạy sau khi bạn đồng ý. Phần mềm kiểm chữ/OCR trước, rồi AI đọc toàn bài và các lần
sửa được kiểm lại. Bản chạy thử một bài thật (390 câu): 24 lần gọi, 576 079 token vào, 53 196 token ra (~20 phút); riêng bằng chứng khoảng 70 000 + 13 000
token mỗi bài. Có sao lưu trước khi chạy; PDF gốc không bị sửa.

Chưa có: video/ảnh làm bằng chứng gốc (chỉ nhập transcript/ghi chú đi kèm); sinh tự động "khoảng trống nghiên cứu ứng viên"; phát hiện
đạo văn (chỉ có dò trùng cách dùng từ).

## Gặp sự cố

- "Another writer holds …": có một tiến trình khác đang ghi vào kho. Đợi nó xong. Chỉ khi chắc chắn tiến trình đó đã chết mới xóa file khóa
  trong `knowledge_store\locks\` (không bao giờ xóa khóa đang sống).
- Trang web không mở: đóng cửa sổ cũ của file số 1 rồi mở lại; trang tự chọn cổng 8765 hoặc cổng trống kế tiếp.
- Thiếu mô hình tìm theo nghĩa hoặc từ điển MeSH: trang tự tải một lần khi khởi động (cần mạng).
- Ghi chú Obsidian không đổi màu graph: mở lại vault.

## An toàn dữ liệu

- PDF gốc không bao giờ bị sửa; phần mềm không tự xóa nguồn.
- Trước các thao tác sửa hàng loạt, bản cũ được lưu (`knowledge_store\backups\`, `concept_history\`, `analysis_history\`, `removed\`).
- Sao lưu thư viện: chép cùng nhau `sources\`, `knowledge_store\`, `vaults\`, `review\`. Chúng không nằm trong git.
