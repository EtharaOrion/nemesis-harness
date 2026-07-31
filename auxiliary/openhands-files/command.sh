docker run \
	--pull=always \
	-e SANDBOX_BASE_CONTAINER_IMAGE={image} \
	-e SANDBOX_VOLUMES={workspace_path}:/workspace:rw \
	-e LOG_ALL_EVENTS=true \
	-e GITHUB_TOKEN={gh-token} \
	-v {docker-socket}:/var/run/docker.sock \
	-v {openhands_files_path}:/openhands-files \
	--add-host host.docker.internal:host-gateway \
	--name openhands-cli-$(date +%Y%m%d%H%M%S) \
	docker.openhands.dev/openhands/openhands:1.2.1 \
	python -m openhands.core.main -f /openhands-files/task-{task-type}-generation.txt --config-file /openhands-files/config.toml
