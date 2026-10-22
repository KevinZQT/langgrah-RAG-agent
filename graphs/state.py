from dataclasses import dataclass, field
from typing import Sequence, Union, Any, Literal, Annotated
from langchain_core.documents import Document
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from uuid import uuid4
import operator
def reduce_docs(
        existing: Sequence[Document] | None,
        new: Union[
            Sequence[Document],
            Sequence[dict[str, Any]],
            Sequence[str],
            str,
            Literal["delete"]]
        ) -> Sequence[Document]:

        if new == "delete":
            return []
        if isinstance(new, str):
            return list(existing or []) + [Document(page_content=new, metadata={"id": str(uuid4())})]

        if isinstance(new, (list, tuple)):
            formatted_new = []
            for each in new:
                if isinstance(each, str):
                    formatted_new.append(
                        Document(page_content=each,  metadata={"id": str(uuid4())})
                    )
                elif isinstance(each, dict):
                     formatted_new.append(
                          Document(**each)
                     )
                elif isinstance(each, Document):
                    formatted_new.append(each)
                else:
                    raise TypeError(f"Unspported file type: {type(each).__name__}")

            return formatted_new

        return existing or []


                     

                    

@dataclass(kw_only=True)
class IndexState:
    docs: Annotated[Sequence[Document], reduce_docs]
    

@dataclass(kw_only=True)
class OverallState(IndexState):
    messages: Annotated[list[BaseMessage], add_messages]
    queries: Annotated[list[str], operator.add] = field(default_factory=list)
    retrieved_docs: list[Document] = field(default_factory=list)
    