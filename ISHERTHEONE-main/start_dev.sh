#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Apex Citadel v3 — Local development startup
# Starts all Phase 0+1 nodes + Redis
# Usage: chmod +x start_dev.sh && ./start_dev.sh
# ═══════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  APEX CITADEL v3 — Starting Phase 0 + Phase 1    ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"

# Check .env
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠ .env not found. Copying from .env.example...${NC}"
    cp .env.example .env
    echo -e "${YELLOW}  → Edit .env with your API keys before trading!${NC}"
fi

# Check Redis
if ! command -v redis-cli &> /dev/null; then
    echo -e "${YELLOW}⚠ redis-cli not found. Start Redis manually on port 6379.${NC}"
elif redis-cli ping &> /dev/null; then
    echo -e "${GREEN}✓ Redis is running${NC}"
else
    echo -e "${YELLOW}⚠ Redis not responding on 6379. Start it: redis-server --daemonize yes${NC}"
fi

PIDS=()

cleanup() {
    echo -e "\n${YELLOW}Shutting down all nodes...${NC}"
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    echo -e "${GREEN}All nodes stopped.${NC}"
}
trap cleanup EXIT INT TERM

# v2 Legacy Nodes
echo -e "${GREEN}Starting Brain Engine (8000)...${NC}"
uvicorn brain_engine:app --host 127.0.0.1 --port 8000 --log-level info &
PIDS+=($!)
sleep 0.5

echo -e "${GREEN}Starting Shadowglass (8001 — v2 legacy)...${NC}"
uvicorn app_shadowglass:app --host 127.0.0.1 --port 8001 --log-level info &
PIDS+=($!)
sleep 0.5

echo -e "${GREEN}Starting Executioner (8002 — v2 legacy)...${NC}"
uvicorn app_executioner:app --host 127.0.0.1 --port 8002 --log-level info &
PIDS+=($!)
sleep 0.5

echo -e "${GREEN}Starting Anti-Rug ML (8003)...${NC}"
uvicorn anti_rug_engine:app --host 127.0.0.1 --port 8003 --log-level info &
PIDS+=($!)
sleep 0.5

# v3 Phase 1 Nodes
echo -e "${CYAN}Starting SpoofHunter L2 (8012)...${NC}"
uvicorn spoofhunter:app --host 127.0.0.1 --port 8012 --log-level info &
PIDS+=($!)
sleep 0.5

echo -e "${CYAN}Starting Newtonian Brother (8011)...${NC}"
uvicorn newtonian:app --host 127.0.0.1 --port 8011 --log-level info &
PIDS+=($!)
sleep 0.5

# v3 Phase 0: Master Orchestrator
echo -e "${CYAN}Starting Master Orchestrator v3 (8007)...${NC}"
MAESTRO_SPOOFHUNTER_URL=http://127.0.0.1:8012 \
MAESTRO_NEWTONIAN_URL=http://127.0.0.1:8011 \
uvicorn maestro_v3:app --host 127.0.0.1 --port 8007 --log-level info &
PIDS+=($!)

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  All nodes online!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Brain Engine         → http://127.0.0.1:8000/health"
echo -e "  Shadowglass (v2)     → http://127.0.0.1:8001/health"
echo -e "  Executioner (v2)     → http://127.0.0.1:8002/health"
echo -e "  Anti-Rug ML          → http://127.0.0.1:8003/health"
echo -e "  ${CYAN}SpoofHunter L2       → http://127.0.0.1:8012/health${NC}"
echo -e "  ${CYAN}Newtonian Brother    → http://127.0.0.1:8011/health${NC}"
echo -e "  ${CYAN}Master Orchestrator  → http://127.0.0.1:8007/health${NC}"
echo ""
echo -e "  Swagger UI           → http://127.0.0.1:8007/docs"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop all nodes.${NC}"

wait
