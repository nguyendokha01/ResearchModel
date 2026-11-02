<!-- GENERATED from config/standard.yaml by `python scripts/kb.py standard --write-docs`. Do not edit by hand. -->

# Quy chuẩn phân tích v1.4 (đã đóng băng) - bản tiếng Việt

Đây là bản tóm tắt cho nhà nghiên cứu duyệt. Mỗi điều khoản là một luật mà mọi AI và mọi chương trình trong dự án phải theo. **Chặn** = vi phạm thì bị từ chối; **Cảnh báo** = chỉ báo cáo. **Đã có code kiểm** = đã có hàm thực hiện luật này, có thể mới kiểm một phần; **chưa có code kiểm** = luật đã ghi nhưng chưa có hàm kiểm tự động. Giai đoạn G3 sẽ đo đầy đủ từng điều khoản. Điều kiện đạt/không đạt đo được nằm trong bản tiếng Anh (`docs/STANDARD.md`).

| Bước | Tên | Số điều khoản | Chặn | Đã có code kiểm |
|---|---|---|---|---|
| IN | Nhập bài và trùng lặp | 10 | 7 | 9 |
| CHR | Ký tự và mã hóa | 8 | 7 | 7 |
| CH | Tách câu, đoạn, bảng, mục | 12 | 11 | 12 |
| EV | Đơn vị bằng chứng (AI) | 23 | 17 | 23 |
| SP | Hồ sơ cấu trúc của bài | 9 | 8 | 9 |
| FG | Đọc hình và bảng | 5 | 4 | 5 |
| ND | Khái niệm và node | 10 | 9 | 10 |
| RL | Quan hệ, mâu thuẫn, mạng có kiểu | 11 | 9 | 11 |
| NC | Thẻ node (AI) | 5 | 3 | 5 |
| VN | Note trong vault | 10 | 8 | 10 |
| RT | Truy xuất | 12 | 7 | 12 |
| AN | Trả lời và bộ kiểm câu trả lời | 20 | 16 | 19 |
| NW | Nhánh mạng neural | 12 | 9 | 9 |
| RQ | Hàng chờ duyệt và quyết định của người | 7 | 6 | 6 |
| SF | An toàn và riêng tư | 7 | 7 | 5 |
| **Tổng** | | **161** | **128** | **152** |

## IN: Nhập bài và trùng lặp

- **IN-01** (Chặn, đã có code kiểm): Trước khi nhập bài, kiểm tra trùng theo thứ tự DOI, PMID, tiêu đề cùng năm, mã băm file; trùng thì gộp thông tin còn thiếu, không tạo bản thứ hai.
- **IN-02** (Chặn, đã có code kiểm): Một bài nằm trong hai vault vẫn là một bản ghi duy nhất, không phải bài trùng.
- **IN-03** (Chặn, đã có code kiểm): File PDF gốc và phụ lục không bao giờ bị sửa; không xóa nếu nhà nghiên cứu chưa đồng ý.
- **IN-04** (Cảnh báo, chưa có code kiểm): Xử lý từng bài một; không nạp cả lô hay cả kho vào bộ nhớ (máy chỉ có 8 GB RAM).
- **IN-05** (Chặn, đã có code kiểm): Mã định danh theo đúng định dạng đã quy ước và không bao giờ dùng lại.
- **IN-06** (Cảnh báo, đã có code kiểm): Khi không mở được PDF gốc, hệ thống ghi rõ điều đó và nói là không kiểm được với file gốc.
- **IN-07** (Cảnh báo, đã có code kiểm): File gốc có thể nằm trên máy, đã lưu trữ, do phần mềm quản lý tài liệu giữ hoặc không còn; hồ sơ luôn đủ thông tin (đường dẫn, mã băm, trạng thái) để tìm lại.
- **IN-08** (Chặn, đã có code kiểm): Tài liệu được trích dẫn mà chưa có trong kho chỉ thành bài 'ma' khi có DOI hoặc có cả tiêu đề và năm; thiếu cả hai thì không tạo.
- **IN-09** (Chặn, đã có code kiểm): File phụ lục chỉ được sao chép (không di chuyển, không sửa), kiểm lại mã băm sau khi sao; trong lô chỉ gắn khi khớp duy nhất.
- **IN-10** (Chặn, đã có code kiểm): File PDF y hệt (cùng mã băm) được gắn vào bài đã có; bản thừa được cách ly để khôi phục được, không xóa.

## CHR: Ký tự và mã hóa

- **CHR-01** (Chặn, đã có code kiểm): Mọi văn bản lưu và sinh ra ở dạng NFC; dạng khác phải chuẩn hóa ngay khi nhận.
- **CHR-02** (Chặn, đã có code kiểm): Câu lưu không được chứa ký tự thay thế, ký tự riêng hay ký tự điều khiển (kể cả 0x08).
- **CHR-03** (Chặn, đã có code kiểm): File văn bản chỉ dùng kiểu xuống dòng LF (riêng file .bat giữ CRLF).
- **CHR-04** (Cảnh báo, đã có code kiểm): Các ký hiệu giống nhau (Δ/∆, °C/◦C/℃) được coi là một khi tìm kiếm; văn bản gốc lưu giữ nguyên.
- **CHR-05** (Chặn, chưa có code kiểm): Ký tự hỏng có thể đổi con số (mất ×, mất P, 10?5...) thì phải đối chiếu PDF hoặc gắn cờ cần kiểm PDF, không đoán.
- **CHR-06** (Chặn, đã có code kiểm): Chỉ sửa ký tự phông khi đã đối chiếu PDF; mã chưa chắc giữ nguyên và bị cảnh báo.
- **CHR-07** (Chặn, đã có code kiểm): Chỉ số mũ viết dạng ^…; máy phân biệt trích dẫn với con số/đơn vị.
- **CHR-08** (Chặn, đã có code kiểm): Trang phải đọc bằng OCR thì ghi rõ cách đọc và độ tin cậy; con số đọc bằng OCR phải đối chiếu với PDF.

## CH: Tách câu, đoạn, bảng, mục

- **CH-01** (Chặn, đã có code kiểm): Không cắt câu sau et al., Fig., e.g., vs., chữ viết tắt tên, số thập phân, hay trước chữ thường.
- **CH-02** (Chặn, đã có code kiểm): Không có mảnh câu mồ côi (đầu viết thường hoặc đuôi cụt) mà không có liên kết nối tiếp đúng.
- **CH-03** (Chặn, đã có code kiểm): Liên kết nối câu chỉ nối hai nửa của cùng một câu; không nối vào ô bảng, tiêu đề hay câu khác.
- **CH-04** (Chặn, đã có code kiểm): Không cắt đoạn giữa chừng một câu.
- **CH-05** (Chặn, đã có code kiểm): Chữ trong bảng tách khỏi văn xuôi; bảng đổ thành câu thì đánh dấu in_table; chú thích/ô bảng không dính vào câu thân bài.
- **CH-06** (Chặn, đã có code kiểm): Bảng phải có tiêu đề cột đúng, không bị chia đôi bản ghi; bảng 0 dòng hoặc thiếu tiêu đề gắn cờ không chắc.
- **CH-07** (Chặn, đã có code kiểm): Mục theo đúng heading in trong bài; bài tổng quan lấy tên mục từ heading con; thân bài không bị gán nhầm vào mục tài liệu tham khảo hay phần đầu trang.
- **CH-08** (Chặn, đã có code kiểm): Nhận chân trang/đầu trang chỉ bằng dấu hiệu cấu trúc, không bằng từ như University hay số lượng chữ số.
- **CH-09** (Chặn, đã có code kiểm): Trích dẫn trong câu bắt đúng (cả tác giả-năm), dấu cơ quan không bị coi là trích dẫn, số tham chiếu có trong danh sách tài liệu.
- **CH-10** (Cảnh báo, đã có code kiểm): Danh sách tài liệu tham khảo: tên có gạch nối giữ nguyên; trường không biết thì để trống.
- **CH-11** (Chặn, đã có code kiểm): Trích lại từng bài, chạy thử trước; chỉ chạy thật khi không mất chữ nào; mã câu cũ được đổi sang mã mới và file cũ được giữ lại.
- **CH-12** (Chặn, đã có code kiểm): Giữ toàn văn tìm được ở mức đầy đủ nhất có thể; không bỏ đoạn nào chỉ vì cho là không quan trọng; chữ đầu/cuối trang rời khỏi đoạn nhưng vẫn giữ trong văn bản trang.

## EV: Đơn vị bằng chứng (AI)

- **EV-01** (Chặn, đã có code kiểm): Mỗi đơn vị bằng chứng có loại hợp lệ, câu tiếng Anh, và 1-6 mã câu có thật của bài, nằm gần nhau, không lấy từ danh sách tài liệu.
- **EV-02** (Chặn, đã có code kiểm): Mọi con số trong phát biểu phải có trong câu nguồn được dẫn, đúng cả dấu âm/dương.
- **EV-03** (Cảnh báo, đã có code kiểm): Loài, loại tế bào, tác nhân, đại lượng, phương pháp phải xuất hiện trong bài.
- **EV-04** (Cảnh báo, đã có code kiểm): Kết quả (result) phải lấy từ phần Results của bài gốc, không lấy từ bài tổng quan.
- **EV-05** (Cảnh báo, đã có code kiểm): Giữ nguyên mức chắc chắn của nguồn: 'có thể/gợi ý/có liên quan' không được thành 'gây ra/chứng minh'.
- **EV-06** (Chặn, đã có code kiểm): Mức chắc chắn tính theo câu mang nhận định chính, không theo cả đơn vị; 'không có ý nghĩa thống kê' là nhóm riêng.
- **EV-07** (Chặn, đã có code kiểm): Câu đang trích bài khác không được làm 'kết quả'; 'cited_statement' phải ghi rõ nguồn trích.
- **EV-08** (Cảnh báo, đã có code kiểm): Hướng thay đổi (tăng/giảm/không đổi) phải khớp câu nguồn.
- **EV-09** (Chặn, đã có code kiểm): Mọi câu thân bài phải được dùng hoặc bỏ có lý do cụ thể.
- **EV-10** (Chặn, đã có code kiểm): Thuật ngữ về một tên chuẩn và một loại; kết quả/phương pháp mới phải có ít nhất một danh sách thuật ngữ.
- **EV-11** (Chặn, đã có code kiểm): Bài nộp phải có phân loại, lý do, loại bằng chứng, người phân tích; bài chưa nối được với vault không bị loại chỉ vì thế.
- **EV-12** (Chặn, đã có code kiểm): Kết quả và phương pháp phải dẫn câu dữ liệu, không dẫn chú thích hay tiêu đề.
- **EV-13** (Chặn, đã có code kiểm): Số đọc từ hình thì không được ghi giá trị; nguồn là bảng thì phải dẫn dòng bảng.
- **EV-14** (Cảnh báo, đã có code kiểm): Một mô hình thứ hai kiểm phát biểu so với câu nguồn; không ủng hộ thì gắn cờ.
- **EV-15** (Chặn, đã có code kiểm): Nếu câu nguồn đã đổi hoặc mất thì đơn vị bị gắn cờ; không tự đoán mã mới.
- **EV-16** (Chặn, đã có code kiểm): AI chỉ đưa mã câu; trích dẫn nguyên văn do code chép, không chèn […] giữa hai nửa của một câu.
- **EV-17** (Chặn, đã có code kiểm): Mỗi kết quả ghi rõ tác nhân, đại lượng, hướng, loài, điều kiện; chỗ nào đọc được thì code điền trước, AI chỉ điền phần mơ hồ.
- **EV-18** (Chặn, đã có code kiểm): Một đơn vị = một kết quả; câu có hai kết quả thì tách hai đơn vị.
- **EV-19** (Chặn, đã có code kiểm): Phát biểu không được thêm nội dung nằm ngoài câu nguồn.
- **EV-20** (Chặn, đã có code kiểm): Đơn vị dựa trên hình bị gắn cờ không nhất quán thì cũng mang cờ đó.
- **EV-21** (Chặn, đã có code kiểm): Chỉ gộp đơn vị giống hệt trong cùng bài và gộp được hoàn tác; khác mô hình, liều, kết quả, mức chắc chắn thì không gộp.
- **EV-22** (Cảnh báo, đã có code kiểm): Từ chỉ mức chắc chắn chỉ có một danh sách duy nhất trong config.
- **EV-23** (Chặn, đã có code kiểm): Tri thức chuẩn (phát biểu, thuật ngữ, trích dẫn) lưu bằng tiếng Anh, không sửa nguyên văn; lời giải thích bằng tiếng Việt không thay thế bản chuẩn.

## SP: Hồ sơ cấu trúc của bài

- **SP-01** (Chặn, đã có code kiểm): Hồ sơ phần mở đầu có đúng 5 mục what, why, who, where, when; mỗi mục có câu nguồn hoặc để trống kèm lý do.
- **SP-02** (Chặn, đã có code kiểm): Văn bản hồ sơ không được thêm hướng ngược, loài, gen, hoặc con số không có trong câu dẫn.
- **SP-03** (Cảnh báo, đã có code kiểm): Mục ai/ở đâu/khi nào giữ nguyên bối cảnh của nguồn; để trống không có nghĩa là không tồn tại trong y văn.
- **SP-04** (Chặn, đã có code kiểm): Hồ sơ quy trình: tên và giá trị là đoạn nguyên văn của Methods theo đúng thứ tự; cái gì không biết thì không đoán.
- **SP-05** (Chặn, đã có code kiểm): Mọi câu Methods được phủ hoặc bỏ có lý do; bài tổng quan không có Methods ghi rõ lý do vắng mặt.
- **SP-06** (Chặn, đã có code kiểm): Tổng quan bài chỉ để tham khảo; mỗi mục có câu nguồn và giữ đúng mức chắc chắn của nguồn.
- **SP-07** (Chặn, đã có code kiểm): Mọi hồ sơ ghi mã nguồn hiện tại; trả lời cho nguồn cũ bị từ chối.
- **SP-08** (Chặn, đã có code kiểm): Bài chỉ 'xong' khi có đủ bằng chứng, 5 mục WH và quy trình; đó là xong về kỹ thuật, không phải khoa học được duyệt.
- **SP-09** (Chặn, đã có code kiểm): Mỗi lượt giao việc tối đa 32 câu / 12000 ký tự, không cắt giữa chuỗi câu nối.

## FG: Đọc hình và bảng

- **FG-01** (Chặn, đã có code kiểm): Đọc hình phải khớp chú thích; cắt sai thì báo crop_wrong và chờ cắt lại.
- **FG-02** (Chặn, đã có code kiểm): Con số trong ghi chú nhất quán phải có trong câu dẫn hoặc giá trị panel; không đọc số từ ảnh.
- **FG-03** (Chặn, đã có code kiểm): Hình lệch chú thích phải hiện ở mọi nơi dùng hình và truyền sang đơn vị bằng chứng liên quan.
- **FG-04** (Cảnh báo, đã có code kiểm): Bản đọc hình chỉ còn hiệu lực khi ảnh cắt và câu dẫn không đổi; ô n/N (%) được tách riêng.
- **FG-05** (Chặn, đã có code kiểm): Bản đọc hình không thay chú thích, bằng chứng trong văn bản hay ảnh cắt; không suy con số từ điểm ảnh.

## ND: Khái niệm và node

- **ND-01** (Chặn, đã có code kiểm): Thuật ngữ phải nối đúng nghĩa theo ngữ cảnh bài (Vitrification không phải Glass Transition, PCA, SIRT...).
- **ND-02** (Chặn, đã có code kiểm): Từ viết tắt do chính bài định nghĩa là cùng một khái niệm với dạng đầy đủ; câu dùng chữ viết tắt cũng được nối vào node.
- **ND-03** (Chặn, đã có code kiểm): Các cách viết khác nhau của một khái niệm gộp thành một node.
- **ND-04** (Chặn, đã có code kiểm): Từ chỉ cách làm nghiên cứu, thuốc thử, cụm từ không được thành node loài hay khái niệm.
- **ND-05** (Cảnh báo, đã có code kiểm): Node chỉ hiện trong vault khi có ít nhất 1 bằng chứng hoặc được nhắc từ 3 lần trở lên.
- **ND-06** (Chặn, đã có code kiểm): Không có node mồ côi hoặc note trống.
- **ND-07** (Chặn, đã có code kiểm): Nối khái niệm từ toàn văn (không từ tài liệu tham khảo hay bảng đổ chữ); mã khái niệm không trùng.
- **ND-08** (Chặn, đã có code kiểm): Chữ viết tắt in hoa phân biệt hoa/thường; bí danh quá ngắn hoặc mơ hồ bị bỏ qua.
- **ND-09** (Chặn, đã có code kiểm): Node chỉ có kiểu chung concept lấy kiểu theo cây MeSH khi mọi mã cây (bỏ A11.284 cấu trúc tế bào) chỉ về một kiểu: A11 tế bào, B01/B03/B04 loài, D phân tử, E05 phương pháp; cây lẫn, kiểu đã cụ thể, topic và node người đã duyệt không đổi.
- **ND-10** (Chặn, đã có code kiểm): Mỗi quyết định của máy về node (đổi kiểu, bỏ node, gỡ MeSH, thêm khái niệm) là một dòng trong node_decisions.jsonl ghi rõ quy tắc hoặc mô hình AI đã quyết, không bao giờ ghi là người đã duyệt; dòng của AI và khái niệm thêm phải dẫn câu gốc trong vault; muốn hoàn tác thì xóa dòng và dựng lại.

## RL: Quan hệ, mâu thuẫn, mạng có kiểu

- **RL-01** (Chặn, đã có code kiểm): Mỗi cạnh tri thức có kiểu và dẫn đúng đơn vị bằng chứng + câu nguồn; cạnh chỉ để điều hướng gắn nav; trong note kết quả chỉ thuộc tính và khối kb:auto cho cạnh có kiểu, link bạn viết ngoài khối là nav.
- **RL-02** (Chặn, đã có code kiểm): Chỉ gọi là mâu thuẫn khi cùng kết quả đo, cùng tác nhân và ngược chiều; tên loài đứng riêng không phải là đại lượng đo.
- **RL-03** (Chặn, đã có code kiểm): Kết luận gộp được tách theo kết quả trước khi ghép; dòng bảng của bài tổng quan được xét như bất đồng trong tài liệu.
- **RL-04** (Chặn, đã có code kiểm): Tên note mâu thuẫn ghi loài của cả hai bên.
- **RL-05** (Chặn, đã có code kiểm): Quan hệ chỉ được viết, rút, đọc trong vault của nó; vault lạ báo lỗi.
- **RL-06** (Chặn, đã có code kiểm): Mâu thuẫn không bị xóa hay ẩn; hiện trong mục Conflicting findings, gắn #conflict; không có ý nghĩa thống kê không có nghĩa là không có tác dụng.
- **RL-07** (Chặn, đã có code kiểm): Quyết định của nhà nghiên cứu về quan hệ không bao giờ bị chạy lại ghi đè.
- **RL-08** (Chặn, đã có code kiểm): Ứng viên khoảng trống nói 'không thấy trong vault', không nói 'chưa có trong y văn'; so khớp theo khái niệm; chỉ nhà nghiên cứu mới duyệt thành knowledge gap.
- **RL-09** (Cảnh báo, đã có code kiểm): Mạng tách cạnh tri thức khỏi cạnh điều hướng; node xuất hiện ở hơn nửa số bài và các bản đồ chỉ mục được đánh dấu để không dùng mở rộng suy luận.
- **RL-10** (Chặn, đã có code kiểm): Note kết quả có chủ thể, vị từ, đối tượng là link node thật và node có link ngược lại.
- **RL-11** (Cảnh báo, đã có code kiểm): Có note so sánh khi từ 2 bài trở lên đo cùng một thứ, dùng lại nhóm đại lượng đã duyệt.

## NC: Thẻ node (AI)

- **NC-01** (Chặn, đã có code kiểm): Mỗi câu trong thẻ node dẫn mã câu nguồn; trường không có nguồn ghi 'không thấy trong vault'; không bỏ sót trường chung.
- **NC-02** (Chặn, đã có code kiểm): Thẻ giữ mức chắc chắn và nguồn gốc (bài gốc, tổng quan, giả thuyết tác giả, bài được trích) của câu nguồn.
- **NC-03** (Chặn, đã có code kiểm): Thẻ còn hiệu lực đến khi câu được trích thay đổi; có bằng chứng mới ngoài danh sách đã ghi thì báo 'có bằng chứng mới'.
- **NC-04** (Cảnh báo, đã có code kiểm): Thẻ có dòng nhỏ tiếng Anh 'AI-written summary from N source sentences, checked automatically.' và card_check: passed; không bắt nhà nghiên cứu duyệt từng thẻ.
- **NC-05** (Cảnh báo, đã có code kiểm): Mỗi lượt viết thẻ tối đa 500 câu; muốn thêm câu cụ thể thì xin theo mã.

## VN: Note trong vault

- **VN-01** (Chặn, đã có code kiểm): Phần mềm chỉ ghi trong khối kb:auto và khóa máy; chữ của người, thư mục Mine và thuộc tính tùy chỉnh không bao giờ bị đổi.
- **VN-02** (Chặn, đã có code kiểm): Mỗi loại note có bộ mục đã duyệt, đúng thứ tự, tiếng Anh; mục rỗng thì ẩn.
- **VN-03** (Chặn, đã có code kiểm): Không có mã máy trong tiêu đề hay thân note; mã chỉ nằm ở front matter và khối Source IDs.
- **VN-04** (Chặn, đã có code kiểm): Note không có ký tự hỏng, chữ giữ chỗ, JSON thô, None; link không khác nhau chỉ ở hoa/thường.
- **VN-05** (Chặn, đã có code kiểm): Mọi link trong vault đều trỏ tới note hoặc ảnh có thật.
- **VN-06** (Chặn, đã có code kiểm): Không có ảnh mồ côi: ảnh nào cũng được note dùng; ảnh lặp hoặc chụp cả trang chú thích (tìm bằng kb.py image-audit) chỉ xóa khi bạn đồng ý trong chat và xóa phải khôi phục được; hình thật thì gắn đúng chỗ.
- **VN-07** (Chặn, đã có code kiểm): Trích dẫn trong note đúng dạng [[Bài|Tác giả Năm, p. N]]; câu trích do code chép; quá cỡ thì bỏ cả dòng, không cắt nửa trích dẫn.
- **VN-08** (Cảnh báo, đã có code kiểm): Phần máy viết dùng một ngôn ngữ nhất quán; không trộn Việt-Anh và không chèn link 127.0.0.1 trong thân note.
- **VN-09** (Cảnh báo, đã có code kiểm): Graph Obsidian tách cạnh tri thức khỏi cạnh điều hướng.
- **VN-10** (Chặn, đã có code kiểm): Note máy bị gỡ thì chuyển vào removed/, không xóa; note có chữ của người thì giữ lại kèm cảnh báo.

## RT: Truy xuất

- **RT-01** (Chặn, đã có code kiểm): Tìm kiếm kết hợp từ khóa, toàn văn, bộ lọc, từ đồng nghĩa và (tùy chọn) nghĩa; độ giống về nghĩa không phải là bằng chứng.
- **RT-02** (Chặn, đã có code kiểm): Khi tìm kiểu OR, xếp theo số nhóm từ khóa khớp trước rồi mới tới bm25.
- **RT-03** (Cảnh báo, đã có code kiểm): Các dạng của một từ (vitrification/vitrified/vitrify) khớp nhau; affect, impact, influence, change là từ dừng.
- **RT-04** (Cảnh báo, đã có code kiểm): Câu hỏi tiếng Việt phải được đổi sang từ khóa tiếng Anh hoặc báo cần dịch.
- **RT-05** (Chặn, đã có code kiểm): Khi độ phủ từ khóa thấp hơn ngưỡng, kết quả phải báo 'không tìm thấy trong vault hiện tại' kèm số nhóm khớp tốt nhất.
- **RT-06** (Chặn, đã có code kiểm): Mặc định chỉ tìm trong vault hiện tại; vault lạ báo lỗi.
- **RT-07** (Chặn, đã có code kiểm): Khi gom bằng chứng, mỗi truy vấn được chia chỗ đều; giữ câu khớp truy vấn thay vì 3 câu đầu của đơn vị.
- **RT-08** (Cảnh báo, đã có code kiểm): Tài liệu tham khảo, phần đầu trang, lời cảm ơn, ghi chú in sau tài liệu tham khảo (notes)... không nằm trong kết quả tìm; phân trang ổn định.
- **RT-09** (Cảnh báo, đã có code kiểm): Với câu hỏi khoa học quan trọng, phải đọc lại ngữ cảnh xung quanh (câu, đoạn, mục) trước khi trả lời.
- **RT-10** (Chặn, đã có code kiểm): Bằng chứng truy xuất luôn kèm mã bài, tiêu đề, DOI/PMID, mục, trang, đoạn gốc và cách tìm ngữ cảnh xung quanh.
- **RT-11** (Cảnh báo, đã có code kiểm): Tìm trùng chữ luôn gọi là 'trùng từ ngữ', không bao giờ kết luận đạo văn.
- **RT-12** (Chặn, đã có code kiểm): Nguồn không phải bài báo (dữ liệu riêng, ghi chú của người) trả riêng, gắn nhãn chưa công bố, không trộn với bài báo.

## AN: Trả lời và bộ kiểm câu trả lời

- **AN-01** (Chặn, đã có code kiểm): Mỗi câu trả lời dẫn ít nhất một mục bằng chứng có thật.
- **AN-02** (Chặn, đã có code kiểm): Con số trong câu trả lời phải có trong nguyên văn mục được dẫn, và mục đó phải nói về đúng đối tượng.
- **AN-03** (Chặn, đã có code kiểm): Câu khẳng định/nhân quả trên nguồn còn dè dặt bị gạch, kể cả các động từ tiếng Việt và chữ viết dạng NFD.
- **AN-04** (Chặn, đã có code kiểm): Nguồn thứ cấp phải ghi rõ tác giả; chỉ tính thứ cấp khi trích dẫn nằm đúng trong câu được dùng.
- **AN-05** (Cảnh báo, đã có code kiểm): Câu chỉ dựa vào bài tổng quan phải nói rõ điều đó.
- **AN-06** (Chặn, đã có code kiểm): Hướng tăng/giảm trong câu trả lời khớp với hướng của đơn vị được dẫn.
- **AN-07** (Chặn, đã có code kiểm): Mức dè dặt của câu nguồn tương ứng phải được giữ; xét theo câu nguồn khớp, nhận cả cách nói 'không có ý nghĩa thống kê'.
- **AN-08** (Chặn, đã có code kiểm): Phần suy luận chỉ được dựa vào note có trong danh sách cho phép.
- **AN-09** (Cảnh báo, đã có code kiểm): Câu mở đầu không phải là 'Có/Không' trần.
- **AN-10** (Chặn, đã có code kiểm): Mỗi định danh trong câu trả lời (mốc histone, loài, tác nhân, điều kiện) phải có trong mục được dẫn.
- **AN-11** (Chặn, đã có code kiểm): Câu nêu giới hạn của bằng chứng được xử lý riêng nhưng không được chứa động từ khẳng định kết quả.
- **AN-12** (Chặn, đã có code kiểm): Số thứ tự mục viết [#n] để không lẫn với trích dẫn tài liệu; từ chối giá trị bool, float, vô cực.
- **AN-13** (Chặn, đã có code kiểm): ask_check kiểm đúng các mục mà ask_prepare đã đưa, kèm lý do gạch từng câu.
- **AN-14** (Chặn, đã có code kiểm): Trích dẫn nguyên văn do code chép; thiếu tác giả/năm/trang thì ghi 'không rõ'.
- **AN-15** (Chặn, đã có code kiểm): Mỗi câu ở phần trả lời và phần suy luận có đúng một loại cơ sở: trích dẫn [#n], 'suy luận từ' các mục hoặc note của server, hoặc nhãn 'kiến thức của AI (không phải bằng chứng trong thư viện)'; kiến thức của AI chỉ ở phần suy luận; 'không thấy trong vault' không có nghĩa là chưa ai nghiên cứu.
- **AN-16** (Cảnh báo, đã có code kiểm): Kết quả mâu thuẫn phải nêu cả hai phía kèm trích dẫn; không âm thầm chọn một bên.
- **AN-17** (Chặn, đã có code kiểm): Thảo luận chỉ lưu khi nhà nghiên cứu nói 'lưu', không phải bằng chứng; knowledge gap chỉ lưu khi họ đồng ý, gồm 3 phần có nhãn; ý chốt chỉ lưu khi họ nói 'chốt', ghi đúng lời họ, ngày và các trích dẫn lúc đó.
- **AN-18** (Chặn, đã có code kiểm): Mỗi nhận định quan trọng truy ngược được: câu trả lời, nhận định, đoạn trích, mục, trang, bài, nguồn gốc.
- **AN-19** (Cảnh báo, chưa có code kiểm): AI không chiều theo ý người hỏi khi không có bằng chứng; phản bác phải kèm trích dẫn và nêu cả hai phía nếu có mâu thuẫn; ý đã chốt thì không phản bác lại, khi hỏi lại thì nêu 'bằng chứng – ý chốt'; bằng chứng mới sau ngày chốt chỉ báo một lần.
- **AN-20** (Chặn, đã có code kiểm): Trí nhớ brainstorm chỉ là ngữ cảnh, không phải bằng chứng: hồ sơ của bạn (chung cho dự án) và ghi chú chủ đề (riêng từng kho) chỉ nằm trong runtime/brainstorm của dự án; khi hiện ra mang nhãn 'ý của bạn (đã nhớ, ngày)' hoặc 'ghi chú tìm kiếm của AI (ngày)', không vào Phần 1-2; chỉ ghi thêm dòng có ngày và người ghi, không sửa dòng cũ, đầy thì từ chối; 'không tìm thấy' đã nhớ phải tìm lại trước khi dùng; không gửi trí nhớ cho Hermes.

## NW: Nhánh mạng neural

- **NW-01** (Chặn, đã có code kiểm): Kết quả neural_prepare không vượt giới hạn dung lượng và token.
- **NW-02** (Chặn, đã có code kiểm): Xếp theo độ liên quan đến câu hỏi; không lặp trường dữ liệu.
- **NW-03** (Chặn, đã có code kiểm): Giữ nhãn loại đơn vị (result, hypothesis...) để phân biệt 'đã thử' với 'giả thuyết'.
- **NW-04** (Cảnh báo, đã có code kiểm): Mở rộng theo bước chỉ đi qua cạnh tri thức, bỏ node trung tâm và bản đồ chỉ mục.
- **NW-05** (Chặn, đã có code kiểm): Đường suy luận tối đa 3 bước, mỗi mắt xích có câu nguồn; không có đường thì báo không tìm thấy. Điểm đầu, điểm cuối và mỗi bước đều là node; note Results/claim/conflict chỉ là bước trung gian; điểm đầu trùng điểm cuối bị từ chối.
- **NW-06** (Chặn, đã có code kiểm): Note trong vault chỉ là ngữ cảnh, không phải chỉ thị hay bằng chứng gốc; neural_check dùng bản chụp của server.
- **NW-07** (Cảnh báo, đã có code kiểm): Mã vé neural dựa trên băm nội dung, không dựa trên giờ sửa file; vé thường quá hạn bị dọn, phiên nghiên cứu đang mở thì không.
- **NW-08** (Cảnh báo, đã có code kiểm): Câu hỏi cần dịch thì neural_prepare báo và dùng truy vấn tiếng Anh.
- **NW-09** (Chặn, đã có code kiểm): 8 câu hỏi chuẩn là bộ đo hồi quy: độ phủ, hạng câu bắt buộc, token, dung lượng; câu V5, M3 phải trả 'không tìm thấy'.
- **NW-10** (Chặn, chưa có code kiểm): Tìm theo từng vòng có giới hạn; mỗi vòng báo câu hỏi WH nào đã có mục trích, WH nào thiếu (đây là cách đoán bằng code) và lý do dừng; WH thiếu thì nói thiếu, không lấy kiến thức của AI để lấp.
- **NW-11** (Chặn, chưa có code kiểm): Phiên nghiên cứu giữ các mục của mọi vòng trên server cho tới khi nhà nghiên cứu đóng phiên hoặc bắt đầu chủ đề mới, không tự đóng theo thời gian; cuộc chat sau có thể mở lại phiên đang mở; bước kiểm câu trả lời so với tất cả các mục đó; vòng nào bị đổi thì bị từ chối; chỉ phiên đã đóng mới được dọn; một vòng có thể chỉ trả mục lục gọn, khi đó Phần 2 chỉ trích những mục đã lấy toàn văn theo số.
- **NW-12** (Chặn, chưa có code kiểm): Mỗi mục tìm được và mỗi trích dẫn có link mở thẳng note trong Obsidian; hình và trang PDF trả về kèm chú thích, trang và link; số AI tự đọc từ hình phải ghi rõ và không phải bằng chứng.

## RQ: Hàng chờ duyệt và quyết định của người

- **RQ-01** (Chặn, đã có code kiểm): Hàng chờ chỉ có 4 nhóm: sai kiến thức bài gốc; sai do AI chưa chắc cách sửa; bài mồ côi chưa nối lại được; nhận định không có nguồn mà máy không quyết được.
- **RQ-02** (Chặn, đã có code kiểm): Không đưa vào hàng chờ: mâu thuẫn, thẻ AI, giả thuyết mở, ảnh cắt sai, câu chưa rõ nghĩa, nhãn/kiểu node, gộp/bỏ node và khái niệm máy tự thêm (máy tự quyết và liệt kê), quyết định xóa hoặc đẩy GitHub.
- **RQ-03** (Chặn, đã có code kiểm): Mỗi mục tự đủ: câu gốc do code chép, link, điều máy phát hiện, đề xuất và 3 ô chọn.
- **RQ-04** (Chặn, chưa có code kiểm): Máy kiểm nguồn trước: nội dung bịa tự bị loại vào removed/; chỉ phần máy không quyết được mới vào hàng chờ.
- **RQ-05** (Chặn, đã có code kiểm): Áp dụng quyết định phải qua bộ kiểm, ghi nhật ký khôi phục được; không sửa PDF — lỗi của bài gốc chỉ gắn cờ source_issue và hiện cảnh báo.
- **RQ-06** (Cảnh báo, đã có code kiểm): File hàng chờ nằm ở gốc vault, là link đầu tiên ở Home, được ghim, đầu trang có 3 dòng hướng dẫn.
- **RQ-07** (Chặn, đã có code kiểm): Chỉ gắn cờ bài khi không có kết nối nào với chủ đề vault; phân loại của người không bị ghi đè; không loại bỏ bài.

## SF: An toàn và riêng tư

- **SF-01** (Chặn, đã có code kiểm): paper_id, vault và limit từ MCP được kiểm trước khi dùng (không '../', vault phải có thật, limit 1..100).
- **SF-02** (Chặn, đã có code kiểm): Thảo luận ghi bằng chế độ chỉ-tạo-mới, chỉ vào vault nghiên cứu, tiêu đề một dòng; không bao giờ đè file.
- **SF-03** (Chặn, chưa có code kiểm): Mọi thay đổi phá hủy phải xác định phạm vi và giữ khôi phục được; không chắc thì cách ly hoặc gắn cờ, không xóa.
- **SF-04** (Chặn, chưa có code kiểm): AI không tự xóa nguồn, đè note của người, duyệt knowledge gap hay loại bài; xong về kỹ thuật không phải được duyệt khoa học.
- **SF-05** (Chặn, đã có code kiểm): Gói chia sẻ/phát hành không chứa khóa API, thư viện riêng, kế hoạch và báo cáo rà soát; leak_count phải bằng 0.
- **SF-06** (Chặn, đã có code kiểm): Mỗi lúc chỉ một tiến trình ghi; việc dài chạy nền có log; không sửa code khi đang dựng lại; kiểm lại Mine/ sau đó.
- **SF-07** (Chặn, đã có code kiểm): Không máy chủ nào ngoài trang tìm kiếm 127.0.0.1 và MCP stdio; không cài thêm thư viện, không gửi bài ra AI ngoài nếu chưa được đồng ý; khóa API chỉ ở secrets.yaml.

## Các quyết định đã chốt (Open decisions)

Bạn đã duyệt các giá trị đề xuất dưới đây.

OD-01. Node 'trung tâm' (hub) là node có mặt trong hơn bao nhiêu phần trăm số bài? Hub bị bỏ khi mở rộng suy luận.
   - Đề xuất: 50% số bài (ví dụ DNA có mặt trong 15/16 bài là hub).
   - Liên quan: RL-09

OD-02. Khi độ phủ nhóm từ khóa của kết quả tốt nhất thấp hơn bao nhiêu thì báo 'không tìm thấy trong vault hiện tại'?
   - Đề xuất: Dưới 0,75 (3/4 số nhóm từ khóa) thì buộc trả lời 'không tìm thấy'.
   - Liên quan: RT-05

OD-03. Giới hạn cho nhánh neural: số token của prompt và dung lượng kết quả.
   - Đề xuất: Fast khoảng 4 000 token, Deep khoảng 8 000 token, dung lượng tối đa 150 KB (hiện 0,7-1,1 MB và 10-15 nghìn token). Kế hoạch có chỗ ghi Fast 4 000, chỗ ghi 6 000: đề xuất 4 000, chỉ nới lên 6 000 nếu độ phủ không đạt.
   - Liên quan: NW-01

OD-04. Mức đạt của bộ 8 câu hỏi chuẩn trên nhánh neural.
   - Đề xuất: Độ phủ ý từ 0,77 trở lên; câu bắt buộc đầu tiên nằm trong 3 mục đầu; V5 và M3 trả 'không tìm thấy'; V3 có ít nhất 3/5 mắt xích.
   - Liên quan: NW-02, NW-09

OD-05. Tìm theo các dạng của một từ (vitrification, vitrified, vitrify): độ tìm lại (recall) tối thiểu.
   - Đề xuất: Từ 0,9 trở lên (hiện chỉ 0,68).
   - Liên quan: RT-03

OD-06. Tỉ lệ kết quả được phép thiếu hướng (tăng/giảm) hoặc thiếu loài sau khi phân tích.
   - Đề xuất: Không quá 5% mỗi loại (hiện có AI để trống tới 94% hướng và 100% loài); phần code điền được thì code điền trước.
   - Liên quan: EV-17

OD-07. Khi nào một node được hiện trong vault? (đã quyết 13/10, xin xác nhận lại)
   - Đề xuất: Có ít nhất 1 đơn vị bằng chứng hoặc được nhắc từ 3 lần trong các bài của vault đó.
   - Liên quan: ND-05

OD-08. Từ chỉ hướng của kết quả: giữ hay đổi sang bộ từ mới?
   - Đề xuất: Giữ bộ hiện có (increase, decrease, no_change, mixed, not_applicable) để khỏi phải đổi dữ liệu cũ; không thêm trường mới.
   - Liên quan: EV-08

OD-09. Phân loại mức tri thức (kết quả / giả thuyết / bối cảnh trích dẫn / diễn giải): dùng trường mới hay dùng loại đơn vị hiện có?
   - Đề xuất: Dùng loại đơn vị hiện có (result, hypothesis, cited_statement, interpretation...), không thêm trường knowledge_level.
   - Liên quan: EV-01, EV-17

OD-10. Hai bài đo cùng một thứ, cùng tác nhân nhưng khác loài (ví dụ H3K9me3 ở bò và ở chuột) có gọi là mâu thuẫn không?
   - Đề xuất: Có, giữ như quyết định 13/10 (là ngách nghiên cứu), nhưng tên note ghi loài của cả hai phía; chỉ tên loài đứng riêng không được coi là đại lượng đo.
   - Liên quan: RL-02, RL-04

OD-11. Ngôn ngữ của phần do máy viết trong note.
   - Đề xuất: Tiêu đề mục và nội dung chuẩn bằng tiếng Anh; tiếng Việt chỉ dùng khi giải thích trong cuộc trò chuyện, không trộn trong một note.
   - Liên quan: VN-08

OD-12. 'Không có ý nghĩa thống kê' có coi là mức dè dặt (hedge) không?
   - Đề xuất: Không: là nhóm riêng 'non_significance'; chỉ ý nói 'có thể/gợi ý' mới là hedge.
   - Liên quan: EV-06, AN-07

OD-13. Mức đạt khi cho một AI khác làm bộ đề chuẩn (kb.py ai-bench), để coi AI đó cho kết quả tương đương.
   - Đề xuất: Qua bộ kiểm ngay lượt đầu từ 80%, trong tối đa 3 lượt từ 95%; giữ hedge 100%; khớp câu từ 0,9; điền đủ trường từ 0,95.
   - Liên quan: không gắn điều khoản

OD-14. Sai sót cắt câu còn lại trên bài thật sau khi trích lại: tối đa bao nhiêu thì chấp nhận?
   - Đề xuất: 0 mảnh câu mồ côi trên bộ đề mẫu; trên kho thật phải báo cáo theo từng bài; lỗi còn lại do thư ký sửa hoặc ghi vào STATUS, không đẩy sang hàng chờ của nhà nghiên cứu.
   - Liên quan: CH-02, CH-05
