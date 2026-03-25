# Data Analyst Agent — Manual Test Plan

## Test Coverage Summary

| Layer | Tests | Automation | Status |
|-------|-------|-----------|--------|
| Unit (mock everything) | 40 | pytest | ✅ Done |
| Integration (real API, mock LLM) | 8 | pytest | ✅ Done |
| **Manual: LLM-in-the-loop** | 12 | Human + real LLM | This document |
| **Manual: UI/UX** | 15 | Human + browser | This document |
| **Manual: Error/Edge cases** | 10 | Human + browser | This document |
| **Manual: Multi-LLM** | 6 | Human + browser | This document |

## Prerequisites

```bash
# 1. All services running
sudo uv run python -m fc_pool_manager.server --config config/fc-pool.yaml --socket /var/run/fc-pool.sock -v &
sudo uv run jupyter kernelgateway --KernelGatewayApp.default_kernel_name=python3-firecracker --KernelGatewayApp.port=8888 &
uv run python -m execution_api.server &
caddy run --config config/Caddyfile &

# 2. Generate test data
uv run python scripts/generate_test_data.py

# 3. Start the chatbot
export ANTHROPIC_API_KEY=sk-ant-...
cd apps/data_analyst
uv run --group apps chainlit run app.py --port 8501

# 4. Open browser to http://localhost:8501
```

---

## Test Suite 1: LLM-in-the-Loop (Real LLM + Real Sandbox)

These tests verify the LLM generates correct code and the agent handles real outputs.

### TC-L01: Basic data summary

| Field | Value |
|-------|-------|
| Precondition | App open, no file uploaded |
| Steps | 1. Upload `test_sales.csv` via drag-and-drop<br>2. Wait for upload confirmation<br>3. Observe LLM auto-analyzes |
| Expected | LLM calls `execute_python_code` to load CSV. Shows df.head(), df.shape, df.dtypes. Mentions row count (~1,098 rows), column names (date, product, region, revenue, units). |
| Pass criteria | Output contains row count AND column names AND sample data |

### TC-L02: Analytical question

| Field | Value |
|-------|-------|
| Precondition | TC-L01 completed (data loaded) |
| Steps | Type: "Which product has the highest total revenue?" |
| Expected | LLM executes groupby + sum, prints results. Names a specific product with a dollar amount. |
| Pass criteria | Answer names a product AND includes a revenue number |

### TC-L03: Visualization request

| Field | Value |
|-------|-------|
| Precondition | TC-L01 completed |
| Steps | Type: "Show me monthly revenue trend as a line chart" |
| Expected | LLM generates matplotlib code. Chart image appears inline in chat. Shows months on x-axis, revenue on y-axis. |
| Pass criteria | PNG image visible in chat message |

### TC-L04: Multi-step analysis

| Field | Value |
|-------|-------|
| Precondition | TC-L01 completed |
| Steps | 1. Type: "Calculate average revenue per unit for each product"<br>2. Type: "Now rank them and show me a bar chart" |
| Expected | Step 1: Prints table with product names + avg revenue/unit. Step 2: Bar chart appears inline, ordered by metric. |
| Pass criteria | Both steps execute successfully, chart shows all products |

### TC-L05: Follow-up with context

| Field | Value |
|-------|-------|
| Precondition | TC-L04 completed |
| Steps | Type: "Filter to just the North region and redo the analysis" |
| Expected | LLM uses existing DataFrame, filters to North, recalculates. References previous analysis. |
| Pass criteria | Output mentions "North" filter AND shows updated numbers different from TC-L04 |

### TC-L06: Dashboard creation

| Field | Value |
|-------|-------|
| Precondition | TC-L01 completed |
| Steps | Type: "Create an interactive dashboard where I can filter by product and region" |
| Expected | LLM calls `launch_dashboard` with Panel code. Iframe appears in chat. Dashboard loads with filter widgets. |
| Pass criteria | Dashboard iframe visible AND interactive (widgets respond to clicks) |

### TC-L07: File export

| Field | Value |
|-------|-------|
| Precondition | TC-L02 completed (analysis done) |
| Steps | Type: "Export the revenue summary as a CSV file" |
| Expected | LLM executes df.to_csv(), then calls download_file. Download button appears in chat. |
| Pass criteria | 📎 file element visible, clicking downloads a valid CSV |

### TC-L08: Statistical analysis

| Field | Value |
|-------|-------|
| Precondition | TC-L01 completed |
| Steps | Type: "Run a correlation analysis between revenue and units" |
| Expected | LLM computes correlation matrix or scatter plot. Shows r-value or correlation coefficient. |
| Pass criteria | Numerical correlation result AND/OR visualization |

### TC-L09: Data cleaning

| Field | Value |
|-------|-------|
| Precondition | Upload a CSV with missing values and duplicates |
| Steps | 1. Upload messy_data.csv (manually add NaN rows + duplicates)<br>2. Type: "Clean this data — handle missing values and duplicates" |
| Expected | LLM detects issues, fills/drops NaN, removes duplicates. Reports what was cleaned. |
| Pass criteria | Output mentions specific counts of cleaned rows |

### TC-L10: Long conversation (context window)

| Field | Value |
|-------|-------|
| Precondition | Fresh chat session |
| Steps | 1. Upload data<br>2. Ask 15+ sequential analysis questions<br>3. On question 16, reference something from question 2 |
| Expected | Agent still functions after 15+ turns. May not remember exact details from early turns (context compaction). Should not crash or error. |
| Pass criteria | No errors after 15+ turns. Agent acknowledges if it lost early context. |

### TC-L11: Complex visualization

| Field | Value |
|-------|-------|
| Precondition | TC-L01 completed |
| Steps | Type: "Create a heatmap of revenue by product and region" |
| Expected | LLM uses seaborn heatmap or matplotlib imshow. Heatmap image appears inline. |
| Pass criteria | Heatmap image with labeled axes visible in chat |

### TC-L12: Multiple file upload

| Field | Value |
|-------|-------|
| Precondition | Fresh chat session |
| Steps | 1. Upload sales_2023.csv and sales_2024.csv simultaneously<br>2. Type: "Compare total revenue between the two years" |
| Expected | Both files uploaded. LLM loads both, computes totals, shows comparison. |
| Pass criteria | Output references both files AND shows comparison numbers |

---

## Test Suite 2: UI/UX

### TC-U01: Welcome screen

| Field | Value |
|-------|-------|
| Steps | Open http://localhost:8501 |
| Expected | Welcome markdown rendered with examples and capabilities list. "Ready!" message appears. |
| Pass criteria | Welcome content visible, no blank screen |

### TC-U02: File drag-and-drop

| Field | Value |
|-------|-------|
| Steps | Drag a CSV file from Finder/Explorer into the chat area |
| Expected | Upload progress shown. "📁 Uploaded" message appears with filename and byte count. |
| Pass criteria | File accepted, confirmation shown |

### TC-U03: File upload via button

| Field | Value |
|-------|-------|
| Steps | Click the attachment/upload button, select a file |
| Expected | Same as TC-U02 |
| Pass criteria | File accepted via button click |

### TC-U04: Tool step visualization

| Field | Value |
|-------|-------|
| Steps | Ask any question that triggers code execution |
| Expected | A collapsible "tool" step appears showing the tool name. Expanding shows the code input and output. |
| Pass criteria | Step element visible with tool name + expandable code |

### TC-U05: Inline image display

| Field | Value |
|-------|-------|
| Steps | Ask for a chart/plot |
| Expected | Image renders inline in the chat message, not as a separate download. |
| Pass criteria | Image visible without clicking a link |

### TC-U06: Dashboard iframe embed

| Field | Value |
|-------|-------|
| Steps | Ask for an interactive dashboard |
| Expected | Iframe appears in chat with Panel app. "open full screen" link visible. |
| Pass criteria | Dashboard interactive within iframe. Pop-out link works. |

### TC-U07: File download element

| Field | Value |
|-------|-------|
| Steps | Ask to export/download a file |
| Expected | 📎 file element appears with filename. Clicking triggers browser download. |
| Pass criteria | Downloaded file is valid and contains expected data |

### TC-U08: Streaming response

| Field | Value |
|-------|-------|
| Steps | Ask a question that produces a long text response |
| Expected | Text appears incrementally (streaming), not all at once after a long wait. |
| Pass criteria | Text visibly streams in over 1-2 seconds |

### TC-U09: Chat input responsiveness

| Field | Value |
|-------|-------|
| Steps | While the agent is processing a tool call, try typing in the input box |
| Expected | Input box remains responsive. User can type while agent works. |
| Pass criteria | No UI freeze during tool execution |

### TC-U10: Browser refresh recovery

| Field | Value |
|-------|-------|
| Steps | 1. Start a conversation with uploaded data<br>2. Refresh the browser (F5)<br>3. Try to continue the conversation |
| Expected | New session starts. Previous context lost. "Ready!" message appears again. Old session auto-cleaned by auto-cull. |
| Pass criteria | App recovers gracefully, no error screen |

### TC-U11: Multiple messages queued

| Field | Value |
|-------|-------|
| Steps | Send a message, then immediately send another before first completes |
| Expected | Messages processed sequentially. No crash or garbled output. |
| Pass criteria | Both responses appear correctly |

### TC-U12: Empty message

| Field | Value |
|-------|-------|
| Steps | Press Enter with empty input |
| Expected | No crash. Either ignored or agent says "Please ask a question." |
| Pass criteria | No error |

### TC-U13: Very long message

| Field | Value |
|-------|-------|
| Steps | Paste a 2000-word analysis request |
| Expected | Agent processes it. May take longer but completes. |
| Pass criteria | Response generated without truncation error |

### TC-U14: Non-data question

| Field | Value |
|-------|-------|
| Steps | Type: "What is the capital of France?" (no data uploaded) |
| Expected | Agent answers directly without trying to execute code, OR politely redirects to data analysis. |
| Pass criteria | No crash, reasonable response |

### TC-U15: Chat history scroll

| Field | Value |
|-------|-------|
| Steps | Generate 20+ messages with charts and tool steps. Scroll up. |
| Expected | Chat history scrollable. Images and steps still render when scrolled into view. |
| Pass criteria | No rendering glitches on scroll |

---

## Test Suite 3: Error and Edge Cases

### TC-E01: Upload oversized file

| Field | Value |
|-------|-------|
| Steps | Upload a file >50MB |
| Expected | "❌ File too large" error message. Chat remains functional. |
| Pass criteria | Clear error, no crash |

### TC-E02: Upload unsupported format

| Field | Value |
|-------|-------|
| Steps | Upload a .zip or .exe file |
| Expected | Rejected by Chainlit file filter OR agent can't process it gracefully. |
| Pass criteria | No crash, clear feedback |

### TC-E03: Malicious code in question

| Field | Value |
|-------|-------|
| Steps | Type: "Run os.system('rm -rf /')" |
| Expected | LLM may generate the code. It executes inside the sandbox VM (isolated). VM is destroyed after session. Host is unaffected. |
| Pass criteria | Host filesystem intact. Session still works after. |

### TC-E04: Infinite loop

| Field | Value |
|-------|-------|
| Steps | Type: "Run while True: pass" |
| Expected | Execution times out (kernel timeout). Agent reports timeout error. |
| Pass criteria | Timeout error within 30s, chat recovers |

### TC-E05: Memory exhaustion

| Field | Value |
|-------|-------|
| Steps | Type: "Create a list with 10 billion elements" |
| Expected | VM runs out of memory. Execution fails. Agent reports error. |
| Pass criteria | Error reported, session may need restart but app doesn't crash |

### TC-E06: Download path traversal attempt

| Field | Value |
|-------|-------|
| Steps | If LLM tries to download /etc/passwd (unlikely but possible) |
| Expected | Agent rejects: "Downloads restricted to /data/." |
| Pass criteria | Rejection message, no file delivered |

### TC-E07: Rapid session creation

| Field | Value |
|-------|-------|
| Steps | Open 5 browser tabs to the chatbot simultaneously |
| Expected | Each tab gets its own session. All function independently until pool exhausts. |
| Pass criteria | At least 3-5 tabs work. Excess tabs get "service unavailable" gracefully. |

### TC-E08: Service restart mid-conversation

| Field | Value |
|-------|-------|
| Steps | 1. Start a conversation<br>2. Kill the Execution API process<br>3. Restart it<br>4. Continue chatting |
| Expected | Next message gets "Session restarted" error. Subsequent messages work with a new session (previous variables lost). |
| Pass criteria | Agent recovers, tells user about lost state |

### TC-E09: Upload CSV with encoding issues

| Field | Value |
|-------|-------|
| Steps | Upload a CSV with Latin-1 encoding (non-UTF8 characters) |
| Expected | LLM detects encoding issue, retries with encoding='latin1' parameter. |
| Pass criteria | Data loads successfully after retry |

### TC-E10: Concurrent tool calls stress

| Field | Value |
|-------|-------|
| Steps | Ask a complex question that triggers 3+ sequential tool calls |
| Expected | All tool calls execute in order. Steps shown correctly in UI. Final answer synthesizes all results. |
| Pass criteria | All steps visible, final text coherent |

---

## Test Suite 4: Multi-LLM Provider

### TC-M01: Claude (default)

| Field | Value |
|-------|-------|
| Steps | Start app with default config. Upload data, ask question. |
| Expected | Claude generates correct code, tools execute, results displayed. |
| Pass criteria | Full flow works end-to-end |

### TC-M02: GPT-4o

| Field | Value |
|-------|-------|
| Steps | Start with `LLM_PROVIDER=openai LLM_MODEL=gpt-4o OPENAI_API_KEY=sk-...` |
| Expected | Same flow as TC-M01 but using GPT-4o. Tool definitions converted correctly. |
| Pass criteria | Full flow works, tool_calls format handled |

### TC-M03: Ollama (local)

| Field | Value |
|-------|-------|
| Steps | Start Ollama with `ollama serve`, then `LLM_PROVIDER=ollama LLM_MODEL=llama3.1` |
| Expected | Local model generates code. Quality may be lower but tools should still work. |
| Pass criteria | Tool calls parsed correctly, code executes |

### TC-M04: Provider switch mid-session

| Field | Value |
|-------|-------|
| Steps | 1. Use Claude for 3 messages<br>2. Stop app, restart with `LLM_PROVIDER=openai`<br>3. Continue analysis |
| Expected | New session starts (provider is set at startup). Previous context lost. |
| Pass criteria | New provider works, no crash from stale state |

### TC-M05: Invalid API key

| Field | Value |
|-------|-------|
| Steps | Start with `ANTHROPIC_API_KEY=invalid-key` |
| Expected | First message fails with authentication error. Clear error shown to user. |
| Pass criteria | Error message mentions authentication, not a stack trace |

### TC-M06: Rate limiting

| Field | Value |
|-------|-------|
| Steps | Send 10 rapid messages (may trigger LLM rate limit) |
| Expected | Agent handles 429 errors. May show "Thinking... (retry)" or delay. |
| Pass criteria | Eventually responds, no permanent failure |

---

## Execution Tracking

| Suite | Total | Pass | Fail | Skip | Tester | Date |
|-------|-------|------|------|------|--------|------|
| LLM-in-the-loop | 12 | | | | | |
| UI/UX | 15 | | | | | |
| Error/Edge cases | 10 | | | | | |
| Multi-LLM | 6 | | | | | |
| **Total** | **43** | | | | | |
