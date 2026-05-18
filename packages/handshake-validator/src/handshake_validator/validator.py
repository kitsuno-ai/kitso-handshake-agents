"""HandshakeValidator — abstract base class for all validator implementations.

A validator is anything that takes a (seeker_card, vacancy_card) pair and
returns a :class:`Verdict`. The protocol does not mandate a model, a rubric,
or a scoring method — only this interface and the verdict contract.

Operators are expected to subclass this and plug in their own classifier.
The package ships one reference subclass, :class:`RuleBasedValidator`, which
is fully deterministic and useful as either a baseline or a no-LLM fallback.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from .verdict import DEFAULT_LOW_SIGNAL_CHARS, Verdict


class HandshakeValidator(ABC):
    """Abstract validator interface.

    Subclasses implement :meth:`validate`. The base class provides shared
    helpers — input shape validation, low-signal detection — so subclasses
    can focus on the classification itself.
    """

    #: Minimum vacancy.description length below which we stamp low_signal=true.
    #: Operators may override this on the subclass when their corpus has a
    #: different distribution.
    low_signal_chars: int = DEFAULT_LOW_SIGNAL_CHARS

    @abstractmethod
    def validate(
        self,
        seeker_card: Mapping[str, Any],
        vacancy_card: Mapping[str, Any],
    ) -> Verdict:
        """Return a verdict for the (seeker, vacancy) pair.

        Parameters
        ----------
        seeker_card:
            A seeker card payload as defined by the v0.2 schema
            (``schemas/v0.2/seeker-card.json``). Both L1 fields (role_targets,
            skills, languages, geography, ...) and L2 fields (experience,
            education, evidence, ...) should be present at this point — the
            validator fires after both sides have exchanged L2 disclosures.
        vacancy_card:
            A vacancy card payload as defined by the v0.2 schema
            (``schemas/v0.2/vacancy-card.json``). L1 + L2 fields combined;
            description in particular should be the **full** L2 description,
            not an L1 excerpt — short descriptions trigger ``low_signal=True``
            but are NOT a reason to fail to produce a verdict.

        Returns
        -------
        Verdict
            Always returns a verdict. Implementations MUST NOT raise on
            classification failure — fall back to ``FitVerdict.WEAK`` with
            ``low_signal=True`` and stamp the reason. The protocol relies on
            every conversation getting a verdict so its state can move on.

        Notes
        -----
        Implementations are strongly encouraged to be **conservative about
        STRONG**: call WEAK when uncertain. The pipeline that strong fits
        land in is the candidate's commitment surface, not a feed. A missed
        STRONG is recoverable (the seeker can broaden their card or wait
        for the next match). A noisy STRONG erodes the trust that makes
        the pipeline worth opening at all.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Helpers — available to subclasses; not required to be used.
    # ------------------------------------------------------------------

    def is_low_signal(self, vacancy_card: Mapping[str, Any]) -> bool:
        """Return ``True`` if the vacancy.description is too short to classify reliably.

        Below ``self.low_signal_chars`` the description is likely a header
        teaser rather than substantive content. Verdicts on low-signal cards
        should be stamped accordingly so downstream analytics can distinguish
        weak-because-thin from weak-because-actually-weak.
        """
        desc = vacancy_card.get("description") or ""
        return len(desc) < self.low_signal_chars
