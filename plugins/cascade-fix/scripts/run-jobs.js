export const meta = {
  name: 'cascade-fix-jobs',
  description: '지적별 작업자·판가름·Claude 검토자 호출',
  phases: [{ title: 'Jobs' }],
}

// args: {jobs: [{id, label, model, effort, kind, promptPath}]}
// kind: worker | checker | adjudicator | review — 아래 SCHEMAS에서 출력 스키마를 고른다
// review는 Claude 검토자(첫 검토·의견·재검토)다. 출력은 {text, model_id}
// 반환: {results: [{id, output}]} — output은 schema를 만족하는 객체, 실패·건너뜀이면 null

// SCHEMAS 시작 (cascade/jobs.py의 SCHEMAS와 같아야 한다)
const SCHEMAS = {
  "worker": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "verdict": {
        "type": "string",
        "enum": [
          "사실",
          "아님",
          "재현 불가",
          "못 정함"
        ]
      },
      "needs_design_decision": {
        "type": "boolean"
      },
      "reason": {
        "type": "string"
      },
      "verify": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "cmd": {
              "type": "string"
            },
            "ok": {
              "type": "boolean"
            }
          },
          "required": [
            "cmd",
            "ok"
          ]
        }
      },
      "model_id": {
        "type": "string"
      }
    },
    "required": [
      "verdict",
      "needs_design_decision",
      "reason",
      "verify",
      "model_id"
    ]
  },
  "checker": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "verdict": {
        "type": "string",
        "enum": [
          "사실",
          "아님",
          "재현 불가",
          "못 정함"
        ]
      },
      "needs_design_decision": {
        "type": "boolean"
      },
      "reason": {
        "type": "string"
      },
      "checker_ok": {
        "type": "boolean"
      },
      "model_id": {
        "type": "string"
      }
    },
    "required": [
      "verdict",
      "needs_design_decision",
      "reason",
      "checker_ok",
      "model_id"
    ]
  },
  "adjudicator": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "verdict": {
        "type": "string",
        "enum": [
          "사실",
          "아님",
          "재현 불가",
          "못 정함"
        ]
      },
      "needs_design_decision": {
        "type": "boolean"
      },
      "reason": {
        "type": "string"
      },
      "verify": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "cmd": {
              "type": "string"
            },
            "ok": {
              "type": "boolean"
            }
          },
          "required": [
            "cmd",
            "ok"
          ]
        }
      },
      "model_id": {
        "type": "string"
      }
    },
    "required": [
      "verdict",
      "needs_design_decision",
      "reason",
      "verify",
      "model_id"
    ]
  },
  "review": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "text": {
        "type": "string"
      },
      "model_id": {
        "type": "string"
      }
    },
    "required": [
      "text",
      "model_id"
    ]
  }
}
// SCHEMAS 끝

const jobs = args?.jobs ?? []
log(`jobs: ${jobs.length}건`)

const results = await pipeline(jobs, j => {
  const schema = SCHEMAS[j.kind]
  if (!schema || !j.promptPath) {
    log(`${j.id}: kind 또는 promptPath가 없어 건너뜀`)
    return Promise.resolve({ id: j.id, output: null })
  }
  return agent(`다음 파일을 Read로 읽고 그 지시를 따른다: ${j.promptPath}`, {
    label: j.label,
    model: j.model,
    effort: j.effort,
    schema,
    phase: 'Jobs',
  }).then(o => ({ id: j.id, output: o }))
})

return { results }
