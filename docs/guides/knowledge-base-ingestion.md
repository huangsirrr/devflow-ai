# DevFlow AI 知识库入库链路：解析、切分与向量写入

> 第一遍怎么读：先看关键流程与验证结果，能说明这个模块怎样工作即可；准备动手时，再沿源码和运行步骤复现。

这一节解读 DevFlow AI 的知识库文档是怎样完成解析、切分与向量入库的。完整链路如下：

```mermaid
flowchart TD
    A[上传原始文件] --> B[校验仓库、大小、类型与重复内容]
    B --> C[保存文件和 queued 记录]
    C --> D[后台提取文本]
    D --> E[按文档结构切分]
    E --> F[按固定 Embedding Contract 生成向量]
    F --> G[PostgreSQL 保存 Document]
    G --> H[Milvus Upsert 向量记录]
    H --> I[原始文档进入 completed]

    B -. 任一步异常 .-> X[PostgreSQL 回滚当前事务]
    D -. 任一步异常 .-> X
    E -. 任一步异常 .-> X
    F -. 任一步异常 .-> X
    G -. 任一步异常 .-> X
    H -. 任一步异常 .-> X
    X --> Y[原始文档进入 failed<br/>并保存 error_message]
```

这条链路有两个容易误判的地方，先放在前面：

1. `completed` 只表示这次任务顺利走完，**不代表 PostgreSQL 和 Milvus 拥有分布式事务**。两套存储之间仍然需要定期对账。
2. 上传接口返回 `202` 时，只表示任务已经接受，**不表示向量已经可用**。页面必须继续轮询文档状态。

---

## 1. 一份知识文件为什么要拆成三类数据

如果只建一张 `documents` 表，把文件名、处理状态、切片正文和向量全放进去，短期看起来简单，后面很快会遇到麻烦：原文件失败时不知道重试哪一步；用户删除文件时不知道它对应多少切片；Milvus 中的向量也无法稳定追踪回原始资料。

DevFlow AI 把一份知识文件拆成三类数据，数据库里再分成两张表：

| 数据 | 保存位置 | 负责什么 |
| --- | --- | --- |
| 原始文件 | `.data/uploads/<repo_id>/` | 保留用户上传的文件内容 |
| `KnowledgeSourceDocument` | PostgreSQL | 保存文件名、校验和、处理状态、错误、字符数和切片数 |
| `Document` | PostgreSQL | 保存真正参与引用的切片正文、标题和 metadata |
| 向量记录 | Milvus | 保存 `Document.id` 对应的 Embedding，并执行向量召回 |

其中，`KnowledgeSourceDocument` 是文件生命周期的主记录，`Document` 是检索和引用的证据单元，Milvus 是向量索引。Milvus 不负责保存上传任务状态，也不替代 PostgreSQL 中的正文和来源信息。

对应模型位于 `backend/app/db/models.py`：

```python
class Document(Base):
    __tablename__ = "documents"

    id = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id = mapped_column(ForeignKey("repositories.id"), index=True)
    source_type = mapped_column(String(80), index=True)
    source_id = mapped_column(Uuid(as_uuid=True), index=True)
    title = mapped_column(String(500))
    content = mapped_column(Text)
    meta = mapped_column("metadata", JSON, default=dict)


class KnowledgeSourceDocument(Base):
    __tablename__ = "knowledge_source_documents"

    id = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id = mapped_column(ForeignKey("repositories.id"), index=True)
    name = mapped_column(String(500))
    status = mapped_column(String(40), default="queued", index=True)
    stored_path = mapped_column(Text)
    checksum = mapped_column(String(64), index=True)
    character_count = mapped_column(Integer, default=0)
    chunk_count = mapped_column(Integer, default=0)
    error_message = mapped_column(Text, nullable=True)
```

两张表通过 `KnowledgeSourceDocument.id -> Document.source_id` 对应起来。删除一份原始文件时，系统可以据此找到它生成的全部切片，而不用依赖文件名猜测。

---

## 2. 上传入口怎样先固定仓库范围

用户在知识库页面选择某个项目并上传文件时，前端调用：

```text
POST /api/rag/repositories/{repo_id}/documents
```

后端入口位于 `backend/app/api/routes/rag.py`。第一步仍然是用 URL 中的 `repo_id` 查询真实 `Repository`：

```python
def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    repo = db.get(Repository, repo_id)
    if repo is None or repo.provider == "knowledge_base":
        raise HTTPException(status_code=404, detail="未找到仓库")
    return repo
```

文件不会自己决定写进哪个项目，上传表单里也没有另一个可以覆盖作用域的 `repo_id`。后面的原始文件目录、数据库记录、`Document` 和 Milvus 记录都会继续使用这个经过校验的仓库 ID。

随后，上传接口按顺序完成四组检查：

```python
data = await file.read(MAX_DOCUMENT_BYTES + 1)
if not data:
    raise HTTPException(status_code=400, detail="上传文件为空")
if len(data) > MAX_DOCUMENT_BYTES:
    raise HTTPException(status_code=400, detail="文件不能超过 12 MB")

filename = _safe_filename(file.filename or "upload.txt")
suffix = Path(filename).suffix.lower()
if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
    raise HTTPException(status_code=415, detail=f"暂不支持 {suffix or '无扩展名'} 文件")
```

多读取一个字节，是为了区分「刚好达到上限」和「已经超过上限」。允许的扩展名来自 `document_processing.py`：

```python
SUPPORTED_KNOWLEDGE_SUFFIXES = {
    ".txt", ".md", ".markdown", ".mdx",
    ".pdf", ".docx", ".json", ".csv", ".log",
}
```

`_safe_filename()` 只保留文件名本身，并把不安全字符替换成下划线：

```python
def _safe_filename(filename: str) -> str:
    value = Path(filename or "upload.txt").name
    return re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+", "_", value).strip("._") or "upload.txt"
```

服务端不会直接把客户端传来的路径拼进上传目录，因此 `../../secret.txt` 不能逃出当前仓库的受控目录。

---

## 3. SHA-256 怎样避免同一仓库重复入库

文件名不能可靠判断内容是否重复。同一份制度文档可能叫 `员工手册.md`，也可能叫 `员工手册-最终版.md`；反过来，同名文件的正文也可能已经更新。

DevFlow AI 对文件字节计算 SHA-256：

```python
checksum = hashlib.sha256(data).hexdigest()
duplicate = (
    db.query(KnowledgeSourceDocument)
    .filter(
        KnowledgeSourceDocument.repo_id == repo_id,
        KnowledgeSourceDocument.checksum == checksum,
        KnowledgeSourceDocument.status != "failed",
    )
    .order_by(KnowledgeSourceDocument.created_at.desc())
    .first()
)
if duplicate is not None:
    return _document_response(duplicate)
```

这段查询同时使用 `repo_id` 和 `checksum`。A 项目上传过的文件，不会阻止 B 项目保存自己的副本；同一仓库中内容相同且未失败的记录，则直接返回旧结果。

失败记录被排除在重复判断之外，因为用户需要重新上传或重试。当前实现没有给 `repo_id + checksum` 增加数据库唯一约束，所以两个完全并发的相同上传仍可能同时通过查询。生产环境如果要严格阻止并发重复，还需要唯一索引或幂等键。

---

## 4. 为什么上传请求只返回 queued

解析 PDF、切分长文档、调用 Embedding 和写 Milvus 都可能耗时。如果全部放在 HTTP 请求里同步执行，页面会长时间等待，网关超时后用户也不知道处理到了哪一步。

上传接口先创建一条 `queued` 记录，再把文件保存到仓库目录：

```python
document = KnowledgeSourceDocument(
    repo_id=repo_id,
    name=filename,
    content_type=file.content_type,
    checksum=checksum,
    file_size=len(data),
    stored_path="pending",
    status="queued",
)
db.add(document)
db.flush()

stored_path = _upload_root(repo_id) / f"{document.id}-{filename}"
await asyncio.to_thread(stored_path.write_bytes, data)
document.stored_path = str(stored_path)
db.commit()

background_tasks.add_task(process_knowledge_document, document.id)
```

`db.flush()` 先拿到文档 UUID，原文件因此可以使用「文档 ID + 安全文件名」命名。文件写入失败时，代码会回滚数据库并尝试删除刚创建的文件，避免只留下半条记录。

接口成功后返回 HTTP `202`。这里的 `202` 表示「任务已经接受」，不表示向量已经可用。前端会继续轮询文档列表，直到状态进入 `completed` 或 `failed`。

需要注意的是，FastAPI `BackgroundTasks` 仍然运行在当前服务进程中。服务在任务执行期间重启，任务不会像持久化队列那样自动恢复；后续要进入生产部署，可以把它迁移到 Celery、RQ 或其他带重试和任务状态的队列。

---

## 5. 后台任务怎样推进文档状态机

后台入口是 `backend/app/services/rag/document_processing.py` 中的 `process_knowledge_document()`。它不能继续使用上传请求的数据库 Session，因为 HTTP 请求结束后原 Session 会被关闭，所以函数重新创建自己的 Session：

```python
def process_knowledge_document(document_id: uuid.UUID) -> None:
    _set_status(document_id, "parsing")
    db = SessionLocal()
    try:
        source = db.get(KnowledgeSourceDocument, document_id)
        if source is None:
            return
        # 读取、解析、切分、Embedding 与写入
    except Exception as exc:
        db.rollback()
        source = db.get(KnowledgeSourceDocument, document_id)
        if source is not None:
            source.status = "failed"
            source.error_message = str(exc)[:1000]
            db.commit()
    finally:
        db.close()
```

完整状态变化是：

```text
queued
  -> parsing
  -> splitting
  -> embedding
  -> completed

任一步抛出异常
  -> failed + error_message
```

状态不是给页面看的装饰字段。它回答了「当前能不能参与检索」：只有处理成功并写完切片与向量的文档才会进入 `completed`；失败记录会保留错误，用户可以修改环境后调用重试接口重新排队。

---

## 6. PDF、DOCX 和文本文件怎样提取正文

`process_knowledge_document()` 先从 `stored_path` 重新读取原文件，并再次检查大小：

```python
with Path(source.stored_path).open("rb") as handle:
    data = handle.read(MAX_DOCUMENT_BYTES + 1)
if len(data) > MAX_DOCUMENT_BYTES:
    raise ValueError(f"knowledge file exceeds the {MAX_DOCUMENT_BYTES} byte limit")

text = extract_text(source.name, data)
if not text.strip():
    raise ValueError("文档中没有可索引的文本")
```

第二次检查很有必要。上传完成以后，磁盘文件可能被误改或替换，后台任务不能假设它和请求阶段读取到的字节永远一致。

具体提取逻辑位于 `backend/app/services/rag/document_extraction.py`：

```python
def extract_document_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(data)
    if suffix == ".docx":
        return _extract_docx(data)
    return decode_text(data)
```

三条路线的职责不同：

- PDF 使用 `pypdf` 逐页提取文字，并插入 `<!-- page: N -->` 标记，让后续切片保留页码。
- DOCX 使用 `python-docx` 按文档顺序读取段落和表格，把 Heading、列表和表格恢复成 Markdown 风格文本。
- TXT、Markdown、JSON、CSV 和日志按 `utf-8-sig -> utf-8 -> gb18030 -> latin-1` 尝试解码。

提取器同时设置了 12 MB 文件上限、400 万字符上限、500 页 PDF 上限，以及 DOCX 压缩包条目数和解压后大小限制。DOCX 本质上是 ZIP 文件，这些检查可以减少压缩炸弹把服务内存耗尽的风险。文本解码还会拒绝前 8192 字节中包含空字节的二进制内容。

---

## 7. `chunk_document()` 怎样按类型选择切分策略

文本提取成功后，任务状态进入 `splitting`，再读取当前仓库的切片配置：

```python
source.status = "splitting"
source.character_count = len(text)
db.commit()

config = get_or_create_config(db, source.repo_id)
chunks = chunk_document(
    text,
    source.name,
    max_chars=config.chunk_size,
    overlap=config.chunk_overlap,
)
```

`get_or_create_config()` 默认提供 `chunk_size=800`、`chunk_overlap=100`，API 还会确保 overlap 小于 chunk size。真正的分发入口位于 `backend/app/services/rag/chunking.py`：

```python
def chunk_document(text, source_path="document.txt", max_chars=1600, overlap=160):
    _validate_limits(max_chars, overlap)
    normalized = _normalize_text(text)
    suffix = Path(source_path).suffix.lower()

    if suffix == ".csv":
        return _csv_chunks(normalized, source_path, max_chars, overlap)
    if suffix in MARKDOWN_SUFFIXES:
        blocks = _markdown_blocks(normalized)
        chunk_type = "markdown_section"
    elif suffix == ".json":
        blocks = _json_blocks(normalized, max_chars)
        chunk_type = "json_value"
    else:
        return _plain_chunks(
            normalized,
            source_path=source_path,
            max_chars=max_chars,
            overlap=overlap,
        )

    chunks = _pack_blocks(...)
    if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
        raise ValueError("document exceeds the chunk limit")
    return chunks
```

这里有一个容易忽略的细节：`MARKDOWN_SUFFIXES` 本身就包含 `.pdf` 和 `.docx`。PDF 和 DOCX 在提取阶段已经被转换成带页码、标题和表格结构的 Markdown 风格文本，所以它们也走结构切分路线，而不是退回普通段落切分。

### Markdown 为什么不会只按字符硬切

`_markdown_blocks()` 会识别标题层级、空行、代码围栏和 PDF 页码。遇到标题时，它先结束上一段，再更新当前章节路径：

```python
heading_match = heading_re.match(line)
if heading_match:
    flush(line_number - 1)
    level = len(heading_match.group(1))
    title = heading_match.group(2).strip()
    while headings and headings[-1][0] >= level:
        headings.pop()
    headings.append((level, title, occurrence))
```

代码围栏会作为一个独立 Block 处理。普通代码块尽量保持完整；单个代码围栏仍然超过预算时，才会进入长代码兜底切分。这样比从第 800 个字符直接截断更适合后续引用。

### 一个切片会保存哪些定位信息

`_emit_chunk()` 不只返回正文，还会生成稳定标识和结构位置：

```python
return {
    "content": content,
    "chunk_id": _stable_id("chunk", parent_id, child_index, content),
    "parent_id": parent_id,
    "child_index": child_index,
    "chunk_type": effective_type,
    "section_path": list(first.section_path),
    "section_title": first.section_path[-1] if first.section_path else None,
    "page": first.page,
    "start_line": ...,
    "end_line": ...,
}
```

`section_path` 让切片知道自己属于「认证 -> 刷新令牌」这样的标题路径；`page`、`start_line` 和 `end_line` 负责定位原文；`chunk_id` 与 `parent_id` 为后续增量更新和父子块扩展提供稳定身份。

JSON 会优先按顶层键或数组元素拆分，并保存 `json_path`。CSV 会重复表头并记录行范围。普通文本则以段落为优先边界，长度不够时再逐步缩小。

---

## 8. Embedding Contract 怎样阻止混用向量空间

切片完成后，系统还不能直接写 Milvus。它先把「文件名 + 章节标题」和正文拼在一起，再按 64 条一批生成 Embedding：

```python
titles = []
for index, chunk in enumerate(chunks):
    label = chunk.get("section_title") or f"Chunk {index + 1}"
    titles.append(f"{source.name} · {label}")

contents = [str(chunk["content"]) for chunk in chunks]
vectors = _embed_in_batches(
    [f"{title}\n{content}" for title, content in zip(titles, contents)],
    provider=config.embedding_provider,
    model=config.embedding_model,
    dimensions=config.embedding_dimensions,
)
```

标题也参与 Embedding，是因为很多问题命中的是章节含义，而不是正文中的完整原句。比如用户问「年假审批」，正文可能只写「至少提前三个工作日提交」，章节标题能为这段规则补上主题。

Embedding Contract（向量模型合同）由 Provider、模型名和维度组成。`backend/app/services/rag/embeddings.py` 会先把仓库配置与全局配置比较：

```python
def validate_embedding_contract(provider: str, model: str, dimensions: int) -> None:
    configured = (
        normalize_embedding_provider(settings.embedding_provider),
        settings.embedding_model.strip(),
        settings.embedding_dimensions,
    )
    requested = (
        normalize_embedding_provider(provider),
        model.strip(),
        dimensions,
    )
    if requested != configured:
        raise ValueError("Embedding contract mismatch: ...")
```

当前所有仓库共享同一个 Milvus Collection，所以也必须共享同一个向量空间。只修改某个仓库的模型名，却继续把向量写进旧 Collection，会让距离分数失去意义。

Provider 分发发生在 `embed_knowledge_texts()`：

```python
if resolved in {"qwen_local", "local"}:
    return _embed_local(...)
if resolved == "dashscope":
    return _embed_openai_compatible(...)
if resolved == "openai_compatible":
    return _embed_openai_compatible(...)
if resolved == "deterministic":
    return [deterministic_embedding(text, dimensions) for text in clean_texts]
```

远程接口返回的每个向量都必须与配置维度一致。代码不会截断或补零，也不会在真实 Provider 故障时偷偷换成 Hash 向量。`deterministic` 只适合测试和演示，必须由配置显式选择。

---

## 9. PostgreSQL 和 Milvus 怎样写入同一批切片

Embedding 成功以后，`process_knowledge_document()` 为每个切片构造 `DocumentPayload`：

```python
payloads = [
    DocumentPayload(
        source_type="knowledge_file",
        source_id=source.id,
        title=title,
        content=content,
        metadata={
            **chunk_metadata,
            "display_title": source.name,
            "filename": source.name,
            "knowledge_document_id": str(source.id),
            "checksum": source.checksum,
            "chunk_index": index,
            "total_chunks": len(chunks),
            "chunk_strategy": "structure_aware_v1",
        },
    )
    for index, (title, content, chunk_metadata) in enumerate(...)
]
```

随后先清理同一原始文件的旧切片，再写入新切片：

```python
delete_document_group(
    db,
    source.repo_id,
    source.id,
    source_type="knowledge_file",
    commit=False,
)
add_documents(
    db,
    source.repo_id,
    payloads,
    commit=False,
    vectors=vectors,
    embedding_info={
        "embedding_provider": config.embedding_provider,
        "embedding_model": config.embedding_model,
        "embedding_dimensions": config.embedding_dimensions,
    },
)
```

`add_documents()` 先把 `Document` 加入 PostgreSQL Session 并 `flush()`，拿到稳定的 `Document.ID`；随后把同一 ID、`repo_id`、`source_type`、正文、metadata 和向量 Upsert 到 Milvus。

Milvus Collection 的 Schema 位于 `backend/app/services/rag/vector_store.py`：

```python
schema.add_field(field_name="pk", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
schema.add_field(field_name="repo_id", datatype=DataType.VARCHAR, max_length=64)
schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
schema.add_field(field_name="source_type", datatype=DataType.VARCHAR, max_length=80)
schema.add_field(field_name="source_id", datatype=DataType.VARCHAR, max_length=64)
schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=500)
schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=8192)
schema.add_field(field_name="metadata_json", datatype=DataType.VARCHAR, max_length=4096)
schema.add_field(field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=self.dimensions)
```

向量索引使用 `COSINE`（余弦相似度）。Collection 已经存在时，`_ensure_collection()` 会读取真实 Schema 并检查 `embedding` 维度；维度不一致就拒绝启动当前向量存储，而不是继续写入。

全部步骤执行完以后，原始文件才进入 `completed`：

```python
source.status = "completed"
source.error_message = None
source.completed_at = datetime.now(timezone.utc)
source.updated_at = source.completed_at
db.commit()
```

### 两套存储之间为什么还需要对账

PostgreSQL 事务不能自动控制 Milvus。当前顺序是「PostgreSQL flush -> Milvus upsert -> PostgreSQL commit」。如果 Milvus 写入失败，数据库事务会回滚，原始文件进入 `failed`；但如果 Milvus 已经写入成功，随后 PostgreSQL commit 失败，向量库里可能暂时留下找不到数据库正文的记录。

同样地，进程在批量 Upsert 中途退出，也可能让两套存储只完成一部分。当前项目提供整仓库 reindex 重建能力，但还没有自动对账任务、候选 Collection 和 Alias 切换。

**所以，`completed` 表示当前任务顺利走完，不代表两套存储已经拥有分布式事务。** 生产环境还需要定期比较 PostgreSQL `Document ID` 与 Milvus `document_id`，发现差异后从原始数据重建。

---

## 10. 删除和重试怎样处理同一组数据

失败文档可以调用：

```text
POST /api/rag/repositories/{repo_id}/documents/{document_id}/retry
```

重试接口会把状态改回 `queued`、清空错误，再次提交后台任务。因为 `process_knowledge_document()` 会先调用 `delete_document_group()`，旧切片会在重新写入前被替换。

删除接口则执行三步：

```python
delete_document_group(
    db,
    repo_id,
    document_id,
    source_type="knowledge_file",
    commit=False,
)
Path(source.stored_path).unlink(missing_ok=True)
db.delete(source)
db.commit()
```

`delete_document_group()` 会按 `repo_id + source_id + source_type` 删除 PostgreSQL `Document`，并把 `Document ID` 和来源条件一起交给 Milvus 删除。最后再删除原始文件和生命周期记录。

当前删除仍然不是跨文件系统、PostgreSQL 和 Milvus 的原子事务。任一步失败都需要保留日志并允许重试，不能只依据 HTTP 状态猜测三个位置一定完全一致。

---

## 11. 前端怎样展示后台处理状态

知识库详情页位于 `frontend/app/knowledge/[id]/page.tsx`。上传函数把多个文件逐个提交：

```tsx
async function uploadFiles(list: FileList | null) {
  if (!list?.length) return;
  setUploading(true);
  try {
    for (const file of Array.from(list)) {
      const form = new FormData();
      form.append("file", file);
      await apiUpload(`/api/rag/repositories/${id}/documents`, form);
    }
    await load();
  } finally {
    setUploading(false);
  }
}
```

只要文档中还有状态不是 `completed` 或 `failed` 的记录，页面每秒重新加载一次：

```tsx
useEffect(() => {
  if (!documents.some((item) => !["completed", "failed"].includes(item.status))) return;
  const timer = window.setInterval(() => load().catch(() => null), 1000);
  return () => window.clearInterval(timer);
}, [documents, load]);
```

文档进入 `completed` 后，点击该行会调用 `chunks` 接口查看实际切片。页面看到的「可用」来自后端状态，不是前端根据等待时间猜出来的。

### 本次复现补齐的两处展示

初稿说「顶部显示 Embedding、Rerank 与 Milvus 是否可用」「预览中可以检查章节标题」。实际检查代码时发现这两个展示都不完整：

- 侧边栏只渲染了 Embedding 和 Rerank，没有 Milvus；
- 切片抽屉只显示「分段 #N」和正文，没有显示 `chunk.title`。

本次已在 `frontend/app/knowledge/[id]/page.tsx` 补齐：

1. `load()` 额外请求 `GET /api/rag/{repo_id}/status`，把 `vector_store` 写入页面状态，侧边栏新增 `Milvus ready · <collection>`（不可用时显示 `degraded` 或错误）。
2. 切片抽屉在正文上方渲染 `chunk.title`，这样「文件名 · 章节标题」可以直接人工核对。

补完后重新执行了 `next build`，并在下面的实际验证中确认两处都正常显示。

---

## 12. 把知识入库链路完整跑一遍

### 12.1 启动依赖和后端

生产口径先启动 PostgreSQL、Milvus 和它依赖的 etcd、MinIO：

```powershell
docker compose up -d postgres etcd minio milvus
```

后端至少需要配置下面几组变量：

```dotenv
DATABASE_URL=postgresql+psycopg://devflow:devflow@localhost:5432/devflow
EMBEDDING_PROVIDER=qwen_local
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_DIMENSIONS=1024
MILVUS_ENABLED=true
MILVUS_URI=http://localhost:19530
MILVUS_COLLECTION=devflow_rag_qwen1024
```

本地 Qwen Embedding 还需要安装可选依赖：

```powershell
cd backend
python -m pip install -r requirements.txt
python -m pip install -r requirements-local-embedding.txt
uvicorn app.main:app --reload
```

如果改用 OpenAI Compatible Embedding，需要把 Provider、模型、维度、Base URL 和 API Key 作为一组配置。已经存在的 Collection 维度不一致时，应使用新的 `MILVUS_COLLECTION` 名称并重建，不能复用旧向量。

### 12.2 本次实测环境

为了在没有 GPU、也没有下载 Qwen 权重的机器上把链路跑通，这次实测使用了下面的组合：

| 项目 | 本次取值 |
| --- | --- |
| 操作系统 | Windows |
| Python | 3.12.14（`backend/.venv`） |
| 元数据库 | SQLite 演示库（由项目自带 `.data/devflow.db` 复制到 `kb-verify/devflow.db`，避免污染原库） |
| 向量库 | 本机 Milvus `localhost:19530`，Collection `devflow_rag_demo1024` |
| Embedding | `deterministic` / `deterministic-demo` / 1024 维 |
| Rerank | 未配置 Qwen Rerank，自动降级为启发式重排 |
| 切片配置 | `chunk_size=800`、`chunk_overlap=100` |

`deterministic` 只用于测试和演示，**不能**替代生产向量空间；但它会完整走同一条 `validate_embedding_contract()`、`add_documents()`、Milvus Upsert 和维度校验路径，所以能验证链路和状态机。生产环境按 12.1 的 `qwen_local` 配置即可，两者的差异只在向量来源。

### 12.3 上传并轮询

取得仓库 UUID 后上传课程示例：

```powershell
$repoId = "替换成当前仓库 UUID"

curl.exe -X POST `
  -F "file=@docs/examples/rag_employee_handbook.md" `
  "http://127.0.0.1:8000/api/rag/repositories/$repoId/documents"
```

第一次响应是 `202` 和 `queued`（本次实测）：

```json
{
  "id": "67425c27-0ebd-4c36-8841-04384fd13d8a",
  "name": "rag_employee_handbook.md",
  "source_type": "upload_file",
  "status": "queued",
  "file_size": 1325,
  "character_count": 0,
  "chunk_count": 0
}
```

继续查询文档列表：

```powershell
$documents = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/rag/repositories/$repoId/documents"

$documents | Select-Object id, name, status, character_count, chunk_count, error_message
```

状态进入 `completed` 后，读取实际切片：

```powershell
$documentId = $documents[0].id

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/rag/repositories/$repoId/documents/$documentId/chunks"
```

本次实测结果：

```text
status = completed
character_count = 543
chunk_count = 5

#1  rag_employee_handbook.md · 星河科技员工手册（RAG 演示资料）  20 字符
#2  rag_employee_handbook.md · 年假申请                        180 字符
#3  rag_employee_handbook.md · 远程办公                        119 字符
#4  rag_employee_handbook.md · 生产故障响应                    143 字符
#5  rag_employee_handbook.md · 费用报销                        138 字符
```

关于 `544` 和 `543` 的差异：`character_count` 统计的是提取出来的原始文本长度（保留 `\r\n`），不是切片正文之和。仓库里这份示例文件是 1325 字节、18 个 CRLF、末尾 1 个 LF；在 Windows 换行下按字节解码得到 543 个字符。旧稿的 544 很可能来自末尾多一个换行的文件版本。本文以当前仓库中的示例文件实测为准；换行风格不同时，这个数字会变化，但 `chunk_count=5` 不变。

Milvus 侧同样有 5 条记录，且 `source_id` 全部等于 `67425c27-0ebd-4c36-8841-04384fd13d8a`（即 `KnowledgeSourceDocument.id`），`document_id` 与 PostgreSQL `Document.id` 一一对应：

```text
count(*) = 5
36a4234c-... | 生产故障响应 | 143 字符
3b76b027-... | 星河科技员工手册（RAG 演示资料） | 20 字符
413b87b3-... | 远程办公 | 119 字符
a5f6158a-... | 费用报销 | 138 字符
c18e5e85-... | 年假申请 | 180 字符
```

### 12.4 重复上传

再次上传同一文件，返回已有记录（同一个 `id`），文档列表仍然只有 1 条。去重查询命中的是 `repo_id + checksum + status != 'failed'`，不会新增第二份成功文档。

### 12.5 失败、重试和删除

上传一份只有空白的 `empty.txt`，后台任务抛出 `ValueError("文档中没有可索引的文本")`，记录进入：

```text
status = failed
error_message = 文档中没有可索引的文本
```

调用重试接口后，状态先回到 `queued`、`error_message` 清空，任务再次执行并以同样的原因失败；这证明重试复用的是同一条生命周期记录，而不是新建文件。

```powershell
POST /api/rag/repositories/$repoId/documents/$documentId/retry
```

删除已完成的文档后，三个位置同时清空：

```text
PostgreSQL knowledge_source_documents = 0
PostgreSQL documents                  = 0
Milvus devflow_rag_demo1024 count     = 0
.data/uploads/<repo_id>/              = 空
```

### 12.6 预期结果清单

- 原始文件记录状态是 `completed`。
- `character_count` 和 `chunk_count` 大于 0（本次为 543 和 5）。
- 每个切片带标题和正文，Markdown 文档还能看到章节路径。
- Milvus 状态是 `ready`，Collection 维度与 Embedding 配置一致。
- 重复上传同一文件时返回已有记录，不新增第二份成功文档。

---

## 13. 用自动化测试验证关键合同

进入后端运行：

```powershell
cd backend
python -m pytest `
  app/tests/test_chunking.py `
  app/tests/test_vector_store.py `
  app/tests/test_rag_source_policy.py `
  -q
```

本次实测：

```text
63 passed in 2.38s
```

这些测试分别证明：

- Markdown 标题、代码围栏、行号、Overlap 和稳定 Chunk ID 不会被普通改动破坏。
- PDF 页数、DOCX 解压大小、二进制文本和总切片数受到限制。
- Embedding 维度不一致时会拒绝写入，不会截断、补零或偷偷换 Provider。
- `knowledge_file` 可以进入 RAG，`code_file` 和未明确允许的来源不能进入向量索引。

复现时发现一个小问题：`test_rag_source_policy.py` 里两个用例依赖「Milvus 已启用」这一环境前提，而仓库的演示 `.env` 默认 `MILVUS_ENABLED=false`，直接跑会失败。已在 `_install_fake_milvus()` 中显式把 `settings.milvus_enabled` 置为 `True`——测试本来就用 Fake Store 替换了 Milvus 客户端，不应该再被环境开关跳过。这样上面这条命令在演示配置下也能稳定通过。

当前测试还没有直接覆盖「上传 API -> BackgroundTasks -> 真实 Milvus」这条完整集成链，所以页面实跑和接口检查仍然必要。后续可以补一个使用测试 Collection 的集成测试，验证成功、失败、重试和删除四条生命周期路径。

---

## 14. 实际验证

### 14.1 空知识库会展示哪些运行条件

打开 DevFlow AI 的「项目知识」页面并进入当前仓库。左侧可以切换文档、召回测试和设置，左侧「运行状态」区显示 Embedding、Rerank 与 Milvus 是否可用。页面能打开只说明 API 正常；只有 Embedding 和 Milvus 都可用，向量入库链才具备运行条件。

![空知识库：Embedding、Rerank 与 Milvus 状态](./assets/knowledge-base-empty.png)

本次截图中 Embedding 是 `deterministic 就绪`、Rerank 是 `启发式重排`、Milvus 是 `ready · devflow_rag_demo1024`。这是 12.2 说明的演示配置；换成 Qwen 后前两项会显示对应 Provider 和模型。

### 14.2 上传文档后状态怎样变化

选择 `docs/examples/rag_employee_handbook.md` 后，后端依次执行保存原文件、解析、切分、Embedding 和 Milvus Upsert。前端持续轮询任务状态，直到文档显示为「可用」。本次验证得到 1 份文档、5 个分段和 543 个有效字符。

![上传完成：1 份文档、5 个分段、543 个字符](./assets/knowledge-base-completed.png)

### 14.3 切片预览能否定位原始内容

点击已经完成的文档，页面通过 `chunks` 接口读取 PostgreSQL 中的 `Document`。预览中可以检查章节标题、片段顺序和正文，确认切片没有只剩一段脱离标题的字符串。

![切片预览：章节标题、顺序和正文](./assets/knowledge-base-chunks.png)

### 14.4 哪些配置会要求重新向量化

设置页中的 Embedding Provider、模型和维度决定向量空间；Chunk Size 和 Overlap 决定切片内容。这两类配置变化后都需要重建索引。Retrieval Method、Top-K 和 Score Threshold 属于查询阶段参数，不会改写已有向量。

![知识库设置：切片、Embedding、检索和重排](./assets/knowledge-base-settings.png)

---

## 15. 结论与生产化清单

这条链路的核心不是「把文件塞进向量库」，而是把一份文件拆成可追踪的三层：

- 原始文件负责可重放；
- `KnowledgeSourceDocument` 负责生命周期和错误；
- `Document` + Milvus 负责检索证据。

只要记住这个分层，状态机、去重、重试、删除和删除后的清理就都能找到对应的数据边界。

进入生产环境前，至少还需要补上：

1. 用 Celery/RQ 等持久化队列替换进程内 `BackgroundTasks`，让重启后可以恢复任务。
2. 给 `repo_id + checksum` 增加唯一索引或幂等键，封住并发重复上传的窗口。
3. Postgres 与 Milvus 的定期对账任务，按 `Document.id` 对比 `document_id`，差异从原始文件重建。
4. 需要重建索引时使用候选 Collection + Alias 切换，而不是直接在旧 Collection 上覆盖。
5. 一条覆盖成功、失败、重试、删除的集成测试，使用独立测试 Collection，不污染生产索引。
