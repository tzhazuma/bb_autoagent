# bb_autoagent

Blackboard Auto-Grading Agent for ShanghaiTech University (上海科技大学).

A Python-based tool that automates assignment grading on Blackboard Learn using LLM (Large Language Models) and batch scoring via Grade Center upload.

## Features

### 1. LLM-Powered Auto Grading
Fetch student submissions from Blackboard, grade them with AI (GPT-4/Claude/DeepSeek) by comparing against standard answers and rubrics, then upload scores back to Grade Center.

```bash
bb-agent grade --course _12345_1 --assignment "Homework 1" \
    --question questions.txt --answer solutions.txt --rubric strict
```

### 2. Batch Grade Upload
Upload scores from CSV/Excel tables (姓名, 学号, 分数) to Blackboard Grade Center - perfect for attendance scores or manually graded assignments.

```bash
bb-agent upload --course _12345_1 --column "签到分" --csv scores.csv
```

### 3. Session Management
Persistent login sessions save time - authenticate once and reuse across multiple operations.

```bash
bb-agent login --username 2022533131
```

## Installation

```bash
git clone https://github.com/tzhazuma/bb_autoagent
cd bb_autoagent
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```env
BB_URL=https://elearning.shanghaitech.edu.cn:8443
BB_USERNAME=你的学号
BB_PASSWORD=你的密码
OPENAI_API_KEY=sk-your-key-here
```

Customize grading rubrics in `prompts/` directory.

## Usage

### Full Auto-Grading Workflow

1. Prepare standard answer file (`solutions.txt`) and question file (`questions.txt`)
2. Run the grading command:

```bash
bb-agent grade \
    --course _12345_1 \
    --assignment "Homework 1" \
    --answer solutions.txt \
    --question questions.txt \
    --rubric default
```

3. The tool will: login → fetch submissions → LLM grade → upload scores → save CSV report

### Batch Upload from CSV

Prepare a CSV file with columns `姓名, 学号, 分数`:

```csv
姓名,学号,分数
张三,2022533001,95
李四,2022533002,88
王五,2022533003,72
```

Then upload:

```bash
bb-agent upload --course _12345_1 --column "签到分" --csv scores.csv
```

### Test Grading (Single Submission)

```bash
bb-agent test-grade \
    --answer solutions.txt \
    --submission student_answer.txt \
    --question questions.txt \
    --rubric strict
```

### Dry-Run Mode

Add `--dry-run` to any command to preview without making changes:

```bash
bb-agent upload --course _12345_1 --column "签到分" --csv scores.csv --dry-run
```

## Grading Rubrics

Two built-in rubrics are provided:

| Rubric | Use Case |
|--------|----------|
| `default` | General assignments with correctness, completeness, clarity, and depth criteria |
| `strict` | STEM assignments with strict answer matching and step-by-step grading |

Custom rubrics can be created as YAML files in the `prompts/` directory.

## Architecture

```
src/
├── auth.py          # Playwright SSO login + session persistence
├── submissions.py   # Fetch/download student submissions
├── grader.py        # LLM grading engine (OpenAI-compatible API)
├── gradebook.py     # Upload grades to Blackboard Grade Center
├── csv_reader.py    # Parse CSV/Excel score tables
├── utils.py         # Config, logging, helpers
└── main.py          # CLI entry point
```

## Technical Details

- **Authentication**: Playwright browser automation handles ShanghaiTech's CAS SSO (ids.shanghaitech.edu.cn)
- **Session Persistence**: Cookies and storage state saved to `session.json` for reuse
- **LLM Integration**: OpenAI-compatible API supporting GPT-4, Claude, DeepSeek, and other providers
- **Grade Center**: Direct DOM interaction with Blackboard's Grade Center via Playwright
- **File Support**: Downloads PDF, DOCX, TXT, Python, Java, C/C++, Jupyter notebooks

## Security Note

Credentials are stored in `.env` (gitignored). Never commit `.env` to version control. Rotate your password regularly.

## License

MIT
