# Wait for the Jupyter Notebook server to start
echo "TIMING - Starting wait at: $(date)"
# (ood_wait_for_port explains a failure on the session card; see ood_checks.sh)
if ! ood_wait_for_port "JupyterLab" "${host}" "${port}" 60; then
  pkill -P ${SCRIPT_PID}
  clean_up 1
fi
echo "TIMING - Wait ended at: $(date)"
sleep 2
