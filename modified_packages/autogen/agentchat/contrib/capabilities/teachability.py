# Copyright (c) 2023 - 2024, Owners of https://github.com/autogen-ai
#
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from  https://github.com/microsoft/autogen are under the MIT License.
# SPDX-License-Identifier: MIT
import os
import pickle
from typing import Dict, Optional, Union

import chromadb
from chromadb.config import Settings

from autogen.agentchat.assistant_agent import ConversableAgent
from autogen.agentchat.contrib.capabilities.agent_capability import AgentCapability
from autogen.agentchat.contrib.text_analyzer_agent import TextAnalyzerAgent

from ....formatting_utils import colored


class Teachability(AgentCapability):
    """
    Teachability uses a vector database to give an agent the ability to remember user teachings,
    where the user is any caller (human or not) sending messages to the teachable agent.
    Teachability is designed to be composable with other agent capabilities.
    To make any conversable agent teachable, instantiate both the agent and the Teachability class,
    then pass the agent to teachability.add_to_agent(agent).
    Note that teachable agents in a group chat must be given unique path_to_db_dir values.

    When adding Teachability to an agent, the following are modified:
    - The agent's system message is appended with a note about the agent's new ability.
    - A hook is added to the agent's `process_last_received_message` hookable method,
    and the hook potentially modifies the last of the received messages to include earlier teachings related to the message.
    Added teachings do not propagate into the stored message history.
    If new user teachings are detected, they are added to new memos in the vector database.
    """

    def __init__(
        self,
        verbosity: Optional[int] = 0,
        reset_db: Optional[bool] = False,
        path_to_db_dir: Optional[str] = "./tmp/teachable_agent_db",
        recall_threshold: Optional[float] = 2.53,
        max_num_retrievals: Optional[int] = 10,
        llm_config: Optional[Union[Dict, bool]] = None,
        collection_name: Optional[str] = "memos",
    ):
        """
        Args:
            verbosity (Optional, int): # 0 (default) for basic info, 1 to add memory operations, 2 for analyzer messages, 3 for memo lists.
            reset_db (Optional, bool): True to clear the DB before starting. Default False.
            path_to_db_dir (Optional, str): path to the directory where this particular agent's DB is stored. Default "./tmp/teachable_agent_db"
            recall_threshold (Optional, float): The maximum distance for retrieved memos, where 0.0 is exact match. Default 1.5. Larger values allow more (but less relevant) memos to be recalled.
            max_num_retrievals (Optional, int): The maximum number of memos to retrieve from the DB. Default 10.
            llm_config (dict or False): llm inference configuration passed to TextAnalyzerAgent.
                If None, TextAnalyzerAgent uses llm_config from the teachable agent.
        """
        self.verbosity = verbosity
        self.path_to_db_dir = path_to_db_dir
        self.recall_threshold = recall_threshold
        self.max_num_retrievals = max_num_retrievals
        self.llm_config = llm_config
        self.collection_name = collection_name 

        self.analyzer = None
        self.teachable_agent = None

        # Create the memo store.
        # self.memo_store = MemoStore(self.verbosity, reset_db, self.path_to_db_dir)
        self.memo_store = MemoStore(
            self.verbosity, 
            reset_db, 
            self.path_to_db_dir,
            collection_name=self.collection_name  # Pass collection name to MemoStore
        )

    def add_to_agent(self, agent: ConversableAgent):
        """Adds teachability to the given agent."""
        self.teachable_agent = agent

        # Register a hook for processing the last message.
        agent.register_hook(hookable_method="process_last_received_message", hook=self.process_last_received_message)

        # Was an llm_config passed to the constructor?
        if self.llm_config is None:
            # No. Use the agent's llm_config.
            self.llm_config = agent.llm_config
        assert self.llm_config, "Teachability requires a valid llm_config."

        # Create the analyzer agent.
        self.analyzer = TextAnalyzerAgent(llm_config=self.llm_config)

        # Append extra info to the system message.
        agent.update_system_message(
            agent.system_message
            + "\nYou've been given the special ability to remember user teachings from prior conversations."
        )

    def prepopulate_db(self):
        """Adds a few arbitrary memos to the DB."""
        self.memo_store.prepopulate()

    def process_last_received_message(self, text: Union[Dict, str]):
        """
        Appends any relevant memos to the message text, and stores any apparent teachings in new memos.
        Uses TextAnalyzerAgent to make decisions about memo storage and retrieval.
        """

        # Try to retrieve relevant memos from the DB.
        expanded_text = text
        if self.memo_store.last_memo_id > 0:
            expanded_text = self._consider_memo_retrieval(text)

        # Try to store any user teachings in new memos to be used in the future.
        self._consider_memo_storage(text)

        # Return the (possibly) expanded message text.
        return expanded_text

    def _consider_memo_storage(self, comment: Union[Dict, str]):
        """Decides whether to store something from one user comment in the DB."""
        memo_added = False

        # Check for a problem-solution pair.
        response = self._analyze(
            comment,
            "Does any part of the TEXT ask the agent to perform a task or solve a problem? Answer with just one word, yes or no.",
        )
        if "yes" in response.lower():
            # Can we extract advice?
            advice = self._analyze(
                comment,
                "Briefly copy any advice from the TEXT that may be useful for a similar but different task in the future. But if no advice is present, just respond with 'none'.",
            )
            if "none" not in advice.lower():
                # Yes. Extract the task.
                task = self._analyze(
                    comment,
                    "Briefly copy just the task from the TEXT, then stop. Don't solve it, and don't include any advice.",
                )
                # Generalize the task.
                general_task = self._analyze(
                    task,
                    "Summarize very briefly, in general terms, the type of task described in the TEXT. Leave out details that might not appear in a similar problem.",
                )
                # Add the task-advice (problem-solution) pair to the vector DB.
                if self.verbosity >= 1:
                    print(colored("\nREMEMBER THIS TASK-ADVICE PAIR", "light_yellow"))
                self.memo_store.add_input_output_pair(general_task, advice)
                memo_added = True

        # Check for information to be learned.
        response = self._analyze(
            comment,
            "Does the TEXT contain information that could be committed to memory? Answer with just one word, yes or no.",
        )
        if "yes" in response.lower():
            # Yes. What question would this information answer?
            question = self._analyze(
                comment,
                "Imagine that the user forgot this information in the TEXT. How would they ask you for this information? Include no other text in your response.",
            )
            # Extract the information.
            answer = self._analyze(
                comment, "Copy the information from the TEXT that should be committed to memory. Add no explanation."
            )
            # Add the question-answer pair to the vector DB.
            if self.verbosity >= 1:
                print(colored("\nREMEMBER THIS QUESTION-ANSWER PAIR", "light_yellow"))
            self.memo_store.add_input_output_pair(question, answer)
            memo_added = True

        # Were any memos added?
        if memo_added:
            # Yes. Save them to disk.
            self.memo_store._save_memos()

    def _consider_memo_retrieval(self, comment: Union[Dict, str]):
        """Decides whether to retrieve memos from the DB, and add them to the chat context."""

        # First, use the comment directly as the lookup key.
        if self.verbosity >= 1:
            print(colored("\nLOOK FOR RELEVANT MEMOS, AS QUESTION-ANSWER PAIRS", "light_yellow"))
        memo_list = self._retrieve_relevant_memos(comment)

        # Next, if the comment involves a task, then extract and generalize the task before using it as the lookup key.
        response = self._analyze(
            comment,
            "Does any part of the TEXT ask the agent to perform a task or solve a problem? Answer with just one word, yes or no.",
        )
        if "yes" in response.lower():
            if self.verbosity >= 1:
                print(colored("\nLOOK FOR RELEVANT MEMOS, AS TASK-ADVICE PAIRS", "light_yellow"))
            # Extract the task.
            task = self._analyze(
                comment, "Copy just the task from the TEXT, then stop. Don't solve it, and don't include any advice."
            )
            # Generalize the task.
            general_task = self._analyze(
                task,
                "Summarize very briefly, in general terms, the type of task described in the TEXT. Leave out details that might not appear in a similar problem.",
            )
            # Append any relevant memos.
            memo_list.extend(self._retrieve_relevant_memos(general_task))

        # De-duplicate the memo list.
        memo_list = list(set(memo_list))

        # Append the memos to the text of the last message.
        return comment + self._concatenate_memo_texts(memo_list)

    def _retrieve_relevant_memos(self, input_text: str) -> list:
        """Returns semantically related memos from the DB."""
        memo_list = self.memo_store.get_related_memos(
            input_text, n_results=self.max_num_retrievals, threshold=self.recall_threshold
        )

        if self.verbosity >= 1:
            # Was anything retrieved?
            if len(memo_list) == 0:
                # No. Look at the closest memo.
                print(colored("\nTHE CLOSEST MEMO IS BEYOND THE THRESHOLD:", "light_yellow"))
                self.memo_store.get_nearest_memo(input_text)
                print()  # Print a blank line. The memo details were printed by get_nearest_memo().

        # Create a list of just the memo output_text strings.
        memo_list = [memo[1] for memo in memo_list]
        return memo_list

    def _concatenate_memo_texts(self, memo_list: list) -> str:
        """Concatenates the memo texts into a single string for inclusion in the chat context."""
        memo_texts = ""
        if len(memo_list) > 0:
            info = "\n# Memories that might help\n"
            for memo in memo_list:
                info = info + "- " + memo + "\n"
            if self.verbosity >= 1:
                print(colored("\nMEMOS APPENDED TO LAST MESSAGE...\n" + info + "\n", "light_yellow"))
            memo_texts = memo_texts + "\n" + info
        return memo_texts

    def _analyze(self, text_to_analyze: Union[Dict, str], analysis_instructions: Union[Dict, str]):
        """Asks TextAnalyzerAgent to analyze the given text according to specific instructions."""
        self.analyzer.reset()  # Clear the analyzer's list of messages.
        self.teachable_agent.send(
            recipient=self.analyzer, message=text_to_analyze, request_reply=False, silent=(self.verbosity < 2)
        )  # Put the message in the analyzer's list.
        self.teachable_agent.send(
            recipient=self.analyzer, message=analysis_instructions, request_reply=True, silent=(self.verbosity < 2)
        )  # Request the reply.
        return self.teachable_agent.last_message(self.analyzer)["content"]


import threading
import fcntl

class MemoStore:
    _instances = {}
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, collection_name: str, **kwargs):
        """Thread-safe singleton instance per collection"""
        with cls._lock:
            if collection_name not in cls._instances:
                cls._instances[collection_name] = cls(collection_name=collection_name, **kwargs)
            return cls._instances[collection_name]

    def __init__(
        self,
        verbosity: Optional[int] = 0,
        reset: Optional[bool] = False,
        path_to_db_dir: Optional[str] = "./tmp/teachable_agent_db",
        collection_name: Optional[str] = "memos"
    ):
        self.verbosity = verbosity
        self.path_to_db_dir = path_to_db_dir
        self.collection_name = collection_name
        self._memo_lock = threading.Lock()  # Lock for memo operations
        self._file_lock = threading.Lock()  # Lock for file operations

        # Load or create the vector DB on disk
        settings = Settings(
            anonymized_telemetry=False,
            allow_reset=True,
            is_persistent=True,
            persist_directory=path_to_db_dir
        )
        self.db_client = chromadb.Client(settings)
        self.vec_db = self.db_client.create_collection(
            self.collection_name,
            get_or_create=True
        )

        self.path_to_dict = os.path.join(
            path_to_db_dir, 
            f"{collection_name}_uid_text_dict.pkl"
        )
        
        self.uid_text_dict = {}
        self.last_memo_id = 0
        
        if (not reset) and os.path.exists(self.path_to_dict):
            self._load_memos()

        if reset:
            self.reset_db()

    def _load_memos(self):
        """Thread-safe memo loading"""
        with self._file_lock:
            try:
                with open(self.path_to_dict, "rb") as f:
                    # Use file locking for extra safety
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    self.uid_text_dict = pickle.load(f)
                    self.last_memo_id = len(self.uid_text_dict)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                print(f"Error loading memos: {e}")
                self.uid_text_dict = {}
                self.last_memo_id = 0

    def _save_memos(self):
        """Thread-safe memo saving"""
        with self._file_lock:
            try:
                with open(self.path_to_dict, "wb") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    pickle.dump(self.uid_text_dict, f)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                print(f"Error saving memos: {e}")

    def add_input_output_pair(self, input_text: str, output_text: str):
        """Thread-safe memo addition"""
        with self._memo_lock:
            self.last_memo_id += 1
            memo_id = str(self.last_memo_id)
            self.vec_db.add(documents=[input_text], ids=[memo_id])
            self.uid_text_dict[memo_id] = (input_text, output_text)
            self._save_memos()

    def get_related_memos(self, query_text: str, n_results: int, threshold: Union[int, float]):
        """Thread-safe memo retrieval"""
        with self._memo_lock:
            if n_results > len(self.uid_text_dict):
                n_results = len(self.uid_text_dict)
            results = self.vec_db.query(query_texts=[query_text], n_results=n_results)
            memos = []
            if results["ids"] and results["ids"][0]:  # Check if we have results
                for i in range(len(results["ids"][0])):
                    uid = results["ids"][0][i]
                    if uid in self.uid_text_dict:  # Verify memo still exists
                        input_text, output_text = self.uid_text_dict[uid]
                        distance = results["distances"][0][i]
                        if distance < threshold:
                            memos.append((input_text, output_text, distance))
            return memos

    def prepopulate(self):
        """Adds a few arbitrary examples to the vector DB, just to make retrieval less trivial."""
        if self.verbosity >= 1:
            print(colored("\nPREPOPULATING MEMORY", "light_green"))
        examples = []
        examples.append({"text": "When I say papers I mean research papers, which are typically pdfs.", "label": "yes"})
        examples.append({"text": "Please verify that each paper you listed actually uses langchain.", "label": "no"})
        examples.append({"text": "Tell gpt the output should still be latex code.", "label": "no"})
        examples.append({"text": "Hint: convert pdfs to text and then answer questions based on them.", "label": "yes"})
        examples.append(
            {"text": "To create a good PPT, include enough content to make it interesting.", "label": "yes"}
        )
        examples.append(
            {
                "text": "No, for this case the columns should be aspects and the rows should be frameworks.",
                "label": "no",
            }
        )
        examples.append({"text": "When writing code, remember to include any libraries that are used.", "label": "yes"})
        examples.append({"text": "Please summarize the papers by Eric Horvitz on bounded rationality.", "label": "no"})
        examples.append({"text": "Compare the h-index of Daniel Weld and Oren Etzioni.", "label": "no"})
        examples.append(
            {
                "text": "Double check to be sure that the columns in a table correspond to what was asked for.",
                "label": "yes",
            }
        )
        for example in examples:
            self.add_input_output_pair(example["text"], example["label"])
        self._save_memos()
