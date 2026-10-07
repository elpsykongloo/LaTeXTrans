from typing import Dict, Any, List
from src.agents.tool_agents.base_tool_agent import BaseToolAgent
from pathlib import Path
import sys
import os
import shutil

from src.utils.progress import st
from src.utils.usage import UsageTracker
from src.utils.paths import project_path
import time

base_dir = os.getcwd()
sys.path.append(base_dir)

 
class GeneratorAgent(BaseToolAgent):
    def __init__(self, 
                 config: Dict[str, Any],
                 project_dir: str = None,
                 output_dir: str = None  # Output directory for parsed files
                 ):
        super().__init__(agent_name="GeneratorAgent", config=config)
        self.config = config
        self.project_dir = project_dir
        self.output_dir = output_dir  # Output directory for parsed files
        self.target_language = config.get("target_language", "ch")
        self.usage = UsageTracker.from_llm_config(
            self.get_llm_config(), project=Path(project_dir).name if project_dir else None
        )
        self.compile_failure = None
        self.repair_report_path = None

    def execute(self) -> Any:
        self.process_b = st.empty()
        with self.process_b:
            self.progress_bar = st.progress(0)
        self.status_text = st.empty()
        
        self.log(f"🤖💬 Start generating for project...⏳: {os.path.basename(self.project_dir)}.")

        self.status_text.text("🔄 Start generating for project...")
        self.progress_bar.progress(5)

        from src.formats.latex.reconstruct import LatexConstructor

        self.status_text.text("📂 Reading...")
        self.progress_bar.progress(10)
        sections = self.read_file(Path(self.output_dir, "sections_map.json"), "json")
        self.progress_bar.progress(20)
        captions = self.read_file(Path(self.output_dir, "captions_map.json"), "json")
        self.progress_bar.progress(30)
        envs = self.read_file(Path(self.output_dir, "envs_map.json"), "json")
        self.progress_bar.progress(40)
        newcommands = self.read_file(Path(self.output_dir, "newcommands_map.json"), "json")
        self.progress_bar.progress(50)
        inputs = self.read_file(Path(self.output_dir, "inputs_map.json"), "json")
        self.progress_bar.progress(60)

        self.status_text.text("📁 Creating translation project directory ..")

        transed_latex_dir = self._creat_transed_latex_folder(self.project_dir)

        self.progress_bar.progress(70)

        print(transed_latex_dir)

        self.status_text.text("🔨 Refactoring LaTeX document...")
        latex_constructor = LatexConstructor(
                                sections=sections,
                                captions=captions,
                                envs=envs,
                                inputs=inputs,
                                newcommands=newcommands,
                                output_latex_dir=transed_latex_dir,
                                target_language=self.target_language,
                            )
        latex_constructor.construct()

        self.progress_bar.progress(80)
        self.status_text.text("🛠️ Compiling PDF document...")

        pdf_file = self._compile_generated_project(transed_latex_dir)

        self.progress_bar.progress(90)
        if pdf_file:

            self.status_text.text("✅ Successfully compiled PDF document.")
            self.progress_bar.progress(100)
            st.success(f"✅ Successfully generated for {os.path.basename(self.project_dir)}.")
            self.process_b.empty()
            self.status_text.empty()

            self.log(f"✅ Successfully generated for {os.path.basename(self.project_dir)}.")
            return pdf_file
        else:
            reason = (self.compile_failure or {}).get("message", "")
            self.status_text.error(f"❌ Failed to compile PDF document. {reason}")
            self.process_b.empty()
            return None

    def _compile_generated_project(self, transed_latex_dir: str):
        """Compile the generated copy and optionally repair localized TeX failures."""
        from src.formats.latex.compile import LaTexCompiler
        from src.formats.latex.repair import LatexCompileRepairAgent
        from src.formats.latex.utils import target_language_family

        compiler = LaTexCompiler(output_latex_dir=transed_latex_dir)
        pdf = compiler.compile_ja() if target_language_family(self.target_language) == "ja" else compiler.compile()
        enabled = str(self.config.get("compile_repair", True)).strip().lower() not in {"false", "0", "no", "off"}
        if not pdf and enabled:
            try:
                repair = LatexCompileRepairAgent(
                    self.config, project_dir=transed_latex_dir,
                    source_dir=self.project_dir, usage=self.usage,
                )
                self.repair_report_path = str(repair.report_path)
                pdf = repair.execute(compiler)
            except Exception as exc:
                # Repair/reporting failures must not hide the real TeX failure or
                # turn it into an unrelated generation error. Do not log credentials.
                self.log(f"编译修复未完成（{type(exc).__name__}），保留原编译失败。", level="warning")
                pdf = None
        self.compile_failure = compiler.last_failure
        return pdf

    def usage_summary(self) -> Dict[str, Any]:
        """Compile-repair usage; stored separately from translation's usage.json."""
        return self.usage.snapshot()

    def _creat_transed_latex_folder(self, src_dir: str) -> str:
        """
        Create a translated folder by copying the source directory and renaming it.
        """
        if not os.path.isdir(src_dir):
            raise NotADirectoryError(f"The path {src_dir} is not a valid directory.")

        source = Path(src_dir).resolve()
        output_root = Path(self.output_dir).resolve()
        dest_path = output_root / source.name
        resolved_dest = dest_path.resolve()
        if (
            resolved_dest.is_relative_to(source) or source.is_relative_to(resolved_dest)
            or not resolved_dest.is_relative_to(output_root) or dest_path.is_symlink()
        ):
            raise ValueError("译文目录必须位于输出目录中，并与源项目目录独立。")

        # Validate before copying: copytree otherwise follows source links and
        # imports files from outside the selected project into the translation.
        for directory, subdirs, files in os.walk(source, followlinks=False):
            for name in (*subdirs, *files):
                project_path(source, Path(directory) / name)

        if dest_path.exists():
            shutil.rmtree(dest_path)
        shutil.copytree(source, dest_path)

        return str(dest_path)
        
    


# import toml
# import argparse

# parser = argparse.ArgumentParser()
# parser.add_argument("--config", type=str, default="config/default.toml")
# args = parser.parse_args()

# config = toml.load(args.config)
# dir = "D:\code\AutoLaTexTrans\output\ch_arXiv-2504.06261v2/arXiv-2504.06261v2"
# Validator = ValidatorAgent(config=config,
#                           project_dir=config["paths"].get("project_dir", None),
#                           validator_dir=dir
#                           )
# Validator.execute()
