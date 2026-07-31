import os
import shutil
from src.gh.commit_analysis.commit_static_analyzer import RepoAnalyzer
from src.utils import run_cmd, pull_image_install_git, create_tmp_container
from src import config
from github import Github, Auth, Repository
import logging

TASK_TYPE_TO_PATHS = {
    'patch': {'original': 'project_original', 'patched': 'project', 'workspace': '/workspace/project'},
    'test': {'original': 'original_repo', 'patched': 'patched_repo', 'workspace': '/workspace'},
}
class OpenHandsRunner:
    def __init__(self):
        self.working_dir = config.openhands['working-dir']
        self.gh_token = config.github['access-token']
        auth = Auth.Token(self.gh_token)
        self.g = Github(auth=auth)

    def _prepare_workspace(self, container_name: str, before_commit: str | None, after_commit: str, task_type: str, pr_number: int | None = None) -> str:
        workspace_path = os.path.join(self.working_dir, 'workspace')

        if os.path.exists(workspace_path):
            cmd = [
                "mv",
                "workspace",
                os.path.join(self.working_dir, f'workspace_{after_commit}_leftover'),
            ]
            run_cmd(cmd, self.working_dir)

        os.makedirs(workspace_path, exist_ok=True)

        patched_path = os.path.join(workspace_path, TASK_TYPE_TO_PATHS[task_type]['patched'])

        cmd = [
            "docker",
            "cp",
            f"{container_name}:/app/original_repo",
            patched_path,
        ]
        run_cmd(cmd, self.working_dir)

        if pr_number is not None:
            run_cmd(["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"], patched_path)

        if task_type == 'test':
            diff = run_cmd(["git", "diff", before_commit if before_commit is not None else f"{after_commit}^", after_commit], patched_path).strip()
            with open(os.path.join(workspace_path, 'diff.patch'), 'w') as f:
                f.write(diff)


        original_path = os.path.join(workspace_path, TASK_TYPE_TO_PATHS[task_type]['original'])
        
        cmd = [
            "docker",
            "cp",
            f"{container_name}:/app/original_repo",
            original_path,
        ]
        run_cmd(cmd, self.working_dir)
        
        return workspace_path

    def _remove_tmp_container(self, container_name: str) -> None:
        cmd = [
            "docker",
            "rm",
            container_name,
        ]
        run_cmd(cmd, self.working_dir)

    def _remove_git_dir(self, workspace_path: str, task_type: str) -> None:
        original_dir = TASK_TYPE_TO_PATHS[task_type]['original']
        patched_dir = TASK_TYPE_TO_PATHS[task_type]['patched']
        cmd = [
            "rm",
            "-rf",
            os.path.join(workspace_path, original_dir, '.git'),
        ]
        run_cmd(cmd, workspace_path)

        cmd = [
            "rm",
            "-rf",
            os.path.join(workspace_path, patched_dir, '.git'),
        ]
        run_cmd(cmd, workspace_path)

    def get_issue_title_and_description(self, repo: Repository, issue_id: int) -> tuple[str, str]:
        issue = repo.get_issue(issue_id)
        return issue.title, issue.body
    
    def get_modified_modules_and_files(self, before_commit: str | None, after_commit: str, repo_path: str) -> tuple[set[str], set[str]]:    
        analyzer = RepoAnalyzer(repo_path)

        if before_commit is not None:
            changed_java_files = analyzer.get_changed_java_src_files_between_commits(before_commit, after_commit)
        else:
            changed_java_files = analyzer.get_changed_java_src_files(after_commit)

        modified_modules = analyzer.get_modules_for_java_files(changed_java_files)
        return modified_modules, changed_java_files


    def _create_task_file(self, repo: Repository, before_commit: str | None, after_commit: str, issue_id: int, repo_path: str, openhands_files_path: str, task_type: str) -> str:
        repo_name = repo.name

        issue_title, issue_description = self.get_issue_title_and_description(repo, issue_id)

        task_file_path = os.path.join(openhands_files_path, f'task-{task_type}-generation.txt')

        task_template = open(os.path.join(config.openhands['openhands-files-dir'], f'task-{task_type}-generation.txt')).read()

        modified_modules, changed_java_files = self.get_modified_modules_and_files(before_commit, after_commit, repo_path)

        task_content = task_template.replace('{issue-title}', issue_title).replace('{issue-description}', issue_description).replace('{buggy-module}', ','.join(modified_modules)).replace('{buggy-files}', '\n'.join(changed_java_files)).replace('{repo-name}', repo_name)

        with open(task_file_path, 'w') as f:
            f.write(task_content)

        return task_file_path


    def _create_config_file(self, openhands_files_path: str, task_type: str) -> str:
        config_template = open(os.path.join(config.openhands['openhands-files-dir'], 'config.toml')).read()
        config_content = config_template.replace('{working-dir}', self.working_dir).replace('{api-key}', config.openhands['llm-api-key']).replace('{model}', config.openhands['llm-model']).replace('{base-url}', config.openhands['llm-base-url']).replace('{max-iterations}', str(config.openhands['max-iterations'])).replace('{workspace}', TASK_TYPE_TO_PATHS[task_type]['workspace'])
        config_file_path = os.path.join(openhands_files_path, 'config.toml')
        with open(config_file_path, 'w') as f:
            f.write(config_content)
        return config_file_path
    
    def _create_command_file(self, image_name: str, workspace_path: str, openhands_files_path: str, task_type: str) -> str:
        command_template = open(os.path.join(config.openhands['openhands-files-dir'], 'command.sh')).read()
        command_content = command_template.replace('{image}', image_name).replace('{workspace_path}', workspace_path).replace('{openhands_files_path}', openhands_files_path).replace('{gh-token}', self.gh_token).replace('{task-type}', task_type).replace('{docker-socket}', config.openhands['docker-socket'])

        command_file_path = os.path.join(self.working_dir, 'command.sh')
        with open(command_file_path, 'w') as f:
            f.write(command_content)

        return command_file_path


    def _prepare_openhands_files(self, image_name: str, repo: Repository, before_commit: str | None, after_commit: str, issue_id: int, workspace_path: str, task_type: str):
        openhands_files_dir = os.path.join(self.working_dir, 'openhands-files')
        os.makedirs(openhands_files_dir, exist_ok=True)

        project_path = os.path.join(workspace_path, TASK_TYPE_TO_PATHS[task_type]['patched'])

        self._create_task_file(repo, before_commit, after_commit, issue_id, project_path, openhands_files_dir, task_type)

        self._create_config_file(openhands_files_dir, task_type)
        self._create_command_file(image_name, workspace_path, openhands_files_dir, task_type)

    def _run_openhands(self) -> None:
        # macOS has no GNU `timeout`; use `gtimeout` (brew coreutils) when
        # present and otherwise run uncapped rather than dying on ENOENT.
        timeout_bin = shutil.which("timeout") or shutil.which("gtimeout")
        if timeout_bin:
            cmd = [timeout_bin, str(config.openhands['llm-timeout'])]
        else:
            logging.warning("No timeout/gtimeout on PATH — running OpenHands without a time cap")
            cmd = []
        cmd += ["bash", "./command.sh"]
        run_cmd(cmd, self.working_dir, capture_output=False)

    def _backup_and_clean_openhands_files(self, commit: str) -> None:
        cmd = [
            "mv",
            os.path.join(self.working_dir, 'workspace'),
            os.path.join(self.working_dir, f'workspace_{commit}'),
        ]
        run_cmd(cmd, self.working_dir)

        # Find containers with images that have 'openhands/runtime' in their name
        cmd = [
            "docker",
            "ps",
            "-a",
            "--format",
            "{{.ID}}\t{{.Image}}",
        ]
        result = run_cmd(cmd, self.working_dir)
        workspace_container_ids = []
        all_container_ids = []
        for line in result.strip().split('\n'):
            if line.strip():
                parts = line.strip().split('\t')
                if len(parts) == 2:
                    container_id, image_name = parts
                    if 'openhands/runtime' in image_name:
                        workspace_container_ids.append(container_id)
                    if 'openhands' in image_name:
                        all_container_ids.append(container_id)
        
        for container_id in all_container_ids:
            # Force remove the container
            cmd = [
                "docker",
                "rm",
                "-f",
                container_id,
            ]
            run_cmd(cmd, self.working_dir)
        
        # Remove all docker images that have 'openhands/runtime' in their name
        cmd = [
            "docker",
            "images",
            "--format",
            "{{.ID}}\t{{.Repository}}:{{.Tag}}",
        ]
        result = run_cmd(cmd, self.working_dir)
        image_ids = set()
        for line in result.strip().split('\n'):
            if line.strip():
                parts = line.strip().split('\t')
                if len(parts) == 2:
                    image_id, image_ref = parts
                    if 'openhands/runtime' in image_ref:
                        image_ids.add(image_id)
        
        for image_id in image_ids:
            cmd = [
                "docker",
                "rmi",
                "-f",
                image_id,
            ]
            run_cmd(cmd, self.working_dir)

    def run_patch_generation(self, repo: str, before_commit: str | None, after_commit: str, issue_id: int, pr_number: int | None = None) -> None:
        logging.info(f"Running patch generation for {repo} {before_commit} {after_commit} {issue_id} {pr_number}")
        image_name = pull_image_install_git(repo, after_commit, self.working_dir)
        logging.info(f"Pulled image {image_name}")

        container_name = create_tmp_container(image_name, self.working_dir)
        logging.info(f"Created temporary container {container_name}")

        workspace_path = self._prepare_workspace(container_name, before_commit, after_commit, 'patch', pr_number)
        logging.info(f"Prepared workspace {workspace_path}")

        repo = self.g.get_repo(repo)

        self._prepare_openhands_files(image_name, repo, before_commit, after_commit, issue_id, workspace_path, 'patch')
        logging.info(f"Prepared openhands files")

        self._remove_tmp_container(container_name)
        self._remove_git_dir(workspace_path, 'patch')
        logging.info(f"Removed git directories")

        self._run_openhands()
        logging.info(f"Ran openhands")

        self._backup_and_clean_openhands_files(after_commit)
        logging.info(f"Backed up and cleaned openhands files")
    
    def run_test_generation(self, repo: str, before_commit: str | None, after_commit: str, issue_id: int, pr_number: int | None) -> None:
        logging.info(f"Running test generation for {repo} {before_commit} {after_commit} {issue_id} {pr_number}")
        image_name = pull_image_install_git(repo, after_commit, self.working_dir)
        logging.info(f"Pulled image {image_name}")
        
        container_name = create_tmp_container(image_name, self.working_dir)
        logging.info(f"Created temporary container {container_name}")

        workspace_path = self._prepare_workspace(container_name, before_commit, after_commit, 'test', pr_number)
        logging.info(f"Prepared workspace {workspace_path}")

        repo = self.g.get_repo(repo)

        self._prepare_openhands_files(image_name, repo, before_commit, after_commit, issue_id, workspace_path, 'test')
        logging.info(f"Prepared openhands files")
        
        self._remove_tmp_container(container_name)
        self._remove_git_dir(workspace_path, 'test')
        logging.info(f"Removed git directories")

        self._run_openhands()
        logging.info(f"Ran openhands")

        self._backup_and_clean_openhands_files(after_commit)
        logging.info(f"Backed up and cleaned openhands files")