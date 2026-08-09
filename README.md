# R2 Case Study — Submission

This repository contains both challenge deliverables.

```
.
├── challenge-1-architecture-strategy/
│   └── R2_Challenge1_Capacity_Tiering_Strategy.pptx   # 4-slide deck + architecture diagram
└── challenge-2-health-monitor/
    ├── monitor.py                  # the health-check script
    ├── config.yaml                 # tunable thresholds
    ├── requirements.txt
    ├── Dockerfile
    ├── storage_api_mock.json       # sample input (from case study appendix)
    ├── sample_output/
    │   ├── alert_output_sample.json
    │   └── alert_output_sample.txt
    └── README.md                   # full build/run/config docs for Challenge 2
```

## Challenge 1 — Capacity & Tiering Strategy

Open `challenge-1-architecture-strategy/R2_Challenge1_Capacity_Tiering_Strategy.pptx`.
4 slides: title, short-term cold-data identification strategy, a hybrid
on-prem → AWS architecture diagram (hot vs. cold data flow), and the tiering
methodology + stakeholder risk-communication plan.

## Challenge 2 — Proactive Storage Health Monitor

See `challenge-2-health-monitor/README.md` for full details. Quick start:

```bash
cd challenge-2-health-monitor
docker build -t storage-health-monitor .
docker run --rm storage-health-monitor
```
