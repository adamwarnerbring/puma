"""Results module for high level API."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from ftag import Cuts, Flavour, Flavours
from ftag.hdf5 import H5Reader

from puma import (
    Histogram,
    HistogramPlot,
    Line2D,
    Line2DPlot,
    Roc,
    RocPlot,
    VarVsEff,
    VarVsEffPlot,
    fraction_scan,
)
from puma.hlplots.tagger import Tagger
from puma.metrics import calc_eff, calc_rej
from puma.utils import get_good_colours, get_good_linestyles, logger


@dataclass
class Results:
    """Store information about several taggers and plot results."""

    signal: Flavour | str
    sample: str
    backgrounds: list = field(init=False)
    atlas_first_tag: str = "Simulation Internal"
    atlas_second_tag: str = None
    atlas_third_tag: str = None
    taggers: dict = field(default_factory=dict)
    perf_vars: str | tuple | list = "pt"
    output_dir: str | Path = "."
    extension: str = "png"
    global_cuts: Cuts | list | None = None
    num_jets: int | None = None
    remove_nan: bool = False

    def __post_init__(self):
        self.set_signal(self.signal)
        if isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)
        if isinstance(self.perf_vars, str):
            self.perf_vars = [self.perf_vars]
        if self.atlas_second_tag is not None and self.atlas_third_tag is not None:
            self.atlas_second_tag = f"{self.atlas_second_tag}\n{self.atlas_third_tag}"

        self.plot_funcs = {
            "probs": self.plot_probs,
            "disc": self.plot_discs,
            "roc": self.plot_rocs,
            "peff": self.plot_var_perf,
            "scan": self.plot_fraction_scans,
        }

    def set_signal(self, signal: Flavour):
        if isinstance(signal, str):
            signal = Flavours[signal]
        self.signal = signal
        if self.signal == Flavours.bjets:
            self.backgrounds = [Flavours.cjets, Flavours.ujets]
        elif self.signal == Flavours.cjets:
            self.backgrounds = [Flavours.bjets, Flavours.ujets]
        elif self.signal == Flavours.hbb:
            self.backgrounds = [Flavours.hcc, Flavours.top, Flavours.qcd]
        elif self.signal == Flavours.hcc:
            self.backgrounds = [Flavours.hbb, Flavours.top, Flavours.qcd]
        else:
            raise ValueError(f"Unsupported signal class {self.signal}.")

    @property
    def flavours(self):
        """Return a list of all flavours.

        Returns
        -------
        list
            List of all flavours
        """
        return [*self.backgrounds, self.signal]

    def add(self, tagger: Tagger):
        """Add tagger to class.

        Parameters
        ----------
        tagger : puma.hlplots.Tagger
            Instance of the puma.hlplots.Tagger class, containing tagger information.

        Raises
        ------
        KeyError
            if model name duplicated
        """
        if str(tagger) in self.taggers:
            raise KeyError(f"{tagger} was already added.")
        if tagger.output_flavours is None:
            tagger.output_flavours = self.flavours
        self.taggers[str(tagger)] = tagger

    def load(self):
        """Iterates all taggers, and loads data if it hasn't already been loaded."""
        req_load = [tagger for tagger in self.taggers.values() if tagger.scores is None]
        tagger_paths = list({tagger.sample_path for tagger in req_load})
        for tp in tagger_paths:
            tp_taggers = [tagger for tagger in req_load if tagger.sample_path == tp]
            self.load_taggers_from_file(
                tp_taggers,
                tp,
                cuts=self.global_cuts,
                num_jets=self.num_jets,
            )

    def add_taggers_from_file(
        self,
        taggers: list[Tagger],
        file_path: Path | str,
        key="jets",
        label_var="HadronConeExclTruthLabelID",
        cuts: Cuts | list | None = None,
        num_jets: int | None = None,
        perf_vars: dict | None = None,
    ):
        """Load one or more taggers from a common file, and adds them to this
        results class.

        Parameters
        ----------
            @self.load_taggers_from_file
        """
        self.load_taggers_from_file(
            taggers,
            file_path,
            key=key,
            label_var=label_var,
            cuts=cuts,
            num_jets=num_jets,
            perf_vars=perf_vars,
        )

        for tagger in taggers:
            self.add(tagger)

    def load_taggers_from_file(  # pylint: disable=R0913
        self,
        taggers: list[Tagger],
        file_path: Path | str,
        key="jets",
        label_var="HadronConeExclTruthLabelID",
        cuts: Cuts | list | None = None,
        num_jets: int | None = None,
        perf_vars: dict | None = None,
    ):
        """Load one or more taggers from a common file. But assumes all taggers
        already been added.

        Parameters
        ----------
        taggers : list[Tagger]
            List of taggers to add
        file_path : str | Path
            Path to file
        key : str, optional
            Key in file, by default 'jets'
        label_var : str
            Label variable to use, by default 'HadronConeExclTruthLabelID'
        cuts : Cuts | list, optional
            Cuts to apply, by default None
        num_jets : int, optional
            Number of jets to load from the file, by default all jets
        perf_vars : dict, optional
            Override the performance variables to use, by default None
        """

        def check_nan(data: np.ndarray) -> np.ndarray:
            """
            Filter out NaN values from loaded data.

            Parameters
            ----------
            data : ndarray
                Data to filter
            """
            mask = np.ones(len(data), dtype=bool)
            for name in data.dtype.names:
                mask = np.logical_and(mask, ~np.isnan(data[name]))
            if np.sum(~mask) > 0:
                if self.remove_nan:
                    logger.warning(
                        f"{np.sum(~mask)} NaN values found in loaded data. Removing" " them."
                    )
                    return data[mask]
                raise ValueError(f"{np.sum(~mask)} NaN values found in loaded data.")
            return data

        # set tagger output nodes
        for tagger in taggers:
            tagger.output_flavours = self.flavours
            if "ftau" in tagger.fxs:
                tagger.output_flavours += [Flavours.taujets]

        # get a list of all variables to be loaded from the file
        if not isinstance(cuts, Cuts):
            cuts = Cuts.empty() if cuts is None else Cuts.from_list(cuts)
        var_list = sum([tagger.variables for tagger in taggers], [label_var])
        var_list += cuts.variables
        var_list += sum([t.cuts.variables for t in taggers if t.cuts is not None], [])
        var_list = list(set(var_list + self.perf_vars))

        # load data
        reader = H5Reader(file_path, precision="full")
        data = reader.load({key: var_list}, num_jets)[key]

        # check for nan values
        data = check_nan(data)
        # apply common cuts
        if cuts:
            idx, data = cuts(data)
            if perf_vars is not None:
                for perf_var_name, perf_var_array in perf_vars.items():
                    perf_vars[perf_var_name] = perf_var_array[idx]

        # for each tagger
        for tagger in taggers:
            sel_data = data
            sel_perf_vars = perf_vars

            # apply tagger specific cuts
            if tagger.cuts:
                idx, sel_data = tagger.cuts(data)

            # attach data to tagger objects
            tagger.extract_tagger_scores(sel_data, source_type="structured_array")
            tagger.labels = np.array(sel_data[label_var], dtype=[(label_var, "i4")])
            if perf_vars is None:
                tagger.perf_vars = {}
                for perf_var in self.perf_vars:
                    if any(x in perf_var for x in ["pt", "mass"]):
                        tagger.perf_vars[perf_var] = sel_data[perf_var] * 0.001
                    else:
                        tagger.perf_vars[perf_var] = sel_data[perf_var]
            else:
                tagger.perf_vars = sel_perf_vars

    def __getitem__(self, tagger_name: str):
        """Retrieve Tagger object.

        Parameters
        ----------
        tagger_name : str
            Name of model

        Returns
        -------
        Tagger
            Instance of the puma.hlplots.Tagger class, containing tagger information.
        """
        return self.taggers[tagger_name]

    def get_filename(self, plot_name: str, suffix: str | None = None):
        """Get output name.

        Parameters
        ----------
        plot_name : str
            plot name
        suffix : str, optional
            suffix to add to output name, by default None

        Returns
        -------
        str
            output name
        """
        base = f"{self.sample}_{self.signal}_{plot_name}"
        if suffix:
            base += f"_{suffix}"
        return Path(self.output_dir / base).with_suffix(f".{self.extension}")

    def plot_probs(
        self,
        suffix: str | None = None,
        **kwargs,
    ):
        """Plot probability distributions.

        Parameters
        ----------
        suffix : str, optional
            Suffix to add to output file name, by default None
        **kwargs : kwargs
            key word arguments for `puma.HistogramPlot`
        """
        line_styles = get_good_linestyles()
        flavours = [*self.backgrounds, self.signal]

        # group by output probability
        for flav_prob in flavours:
            histo = HistogramPlot(
                n_ratio_panels=1,
                xlabel=flav_prob.px,
                ylabel="Normalised number of jets",
                figsize=(7.0, 4.5),
                atlas_first_tag=self.atlas_first_tag,
                atlas_second_tag=self.atlas_second_tag,
                **kwargs,
            )

            tagger_labels = []
            for i, tagger in enumerate(self.taggers.values()):
                tagger_labels.append(tagger.label if tagger.label else tagger.name)
                for flav_class in flavours:
                    histo.add(
                        Histogram(
                            tagger.probs(flav_prob, flav_class),
                            ratio_group=flav_class,
                            label=flav_class.label if i == 0 else None,
                            colour=flav_class.colour,
                            linestyle=line_styles[i],
                        ),
                        reference=tagger.reference,
                    )

            histo.draw()
            histo.make_linestyle_legend(
                linestyles=line_styles,
                labels=tagger_labels,
                bbox_to_anchor=(0.55, 1),
            )
            histo.savefig(self.get_filename(f"probs_{flav_prob.px}", suffix))

        # group by flavour
        for flav_class in flavours:
            histo = HistogramPlot(
                n_ratio_panels=1,
                xlabel=flav_class.label,
                ylabel="Normalised number of jets",
                figsize=(7.0, 4.5),
                atlas_first_tag=self.atlas_first_tag,
                atlas_second_tag=self.atlas_second_tag,
                **kwargs,
            )

            tagger_labels = []
            for i, tagger in enumerate(self.taggers.values()):
                tagger_labels.append(tagger.label if tagger.label else tagger.name)
                for flav_prob in flavours:
                    histo.add(
                        Histogram(
                            tagger.probs(flav_prob, flav_class),
                            ratio_group=flav_prob,
                            label=flav_prob.px if i == 0 else None,
                            colour=flav_prob.colour,
                            linestyle=line_styles[i],
                        ),
                        reference=tagger.reference,
                    )

            histo.draw()
            histo.make_linestyle_legend(
                linestyles=line_styles,
                labels=tagger_labels,
                bbox_to_anchor=(0.55, 1),
            )
            histo.savefig(self.get_filename(f"probs_{flav_class}", suffix))

    def plot_discs(
        self,
        suffix: str | None = None,
        exclude_tagger: list | None = None,
        xlabel: str | None = None,
        wp_vlines: list | None = None,
        **kwargs,
    ):
        """Plot discriminant distributions.

        Parameters
        ----------
        suffix : str, optional
            Suffix to add to output file name, by default None
        exclude_tagger : list, optional
            List of taggers to be excluded from this plot, by default None
        xlabel : str, optional
            x-axis label, by default "$D_{b}$"
        wp_vlines : list, optional
            List of WPs to draw vertical lines at, by default None
        **kwargs : kwargs
            key word arguments for `puma.HistogramPlot`
        """
        if xlabel is None:
            xlabel = rf"$D_{{{self.signal.name.rstrip('jets')}}}$"
        if wp_vlines is None:
            wp_vlines = []

        line_styles = get_good_linestyles()
        hist_defaults = {
            "n_ratio_panels": 0,
            "xlabel": xlabel,
            "ylabel": "Normalised number of jets",
            "figsize": (7.0, 4.5),
            "atlas_first_tag": self.atlas_first_tag,
            "atlas_second_tag": self.atlas_second_tag,
        }
        if kwargs is not None:
            hist_defaults.update(kwargs)
        histo = HistogramPlot(**hist_defaults)

        tagger_labels = []
        for i, tagger in enumerate(self.taggers.values()):
            if exclude_tagger is not None and tagger.name in exclude_tagger:
                continue
            discs = tagger.discriminant(self.signal)

            wp_cuts, wp_labels = [], []
            # get working point
            for wp in wp_vlines:
                cut = np.percentile(discs[tagger.is_flav(self.signal)], 100 - wp)
                label = None if i > 0 else f"{wp}%"
                wp_cuts.append(cut)
                wp_labels.append(label)

            histo.draw_vlines(wp_cuts, labels=wp_labels, linestyle=line_styles[i])
            for flav in self.flavours:
                histo.add(
                    Histogram(
                        discs[tagger.is_flav(flav)],
                        ratio_group=flav,
                        label=flav.label if i == 0 else None,
                        colour=flav.colour,
                        linestyle=line_styles[i],
                    ),
                    reference=tagger.reference,
                )
            tagger_labels.append(tagger.label if tagger.label else tagger.name)
        histo.draw()
        histo.make_linestyle_legend(
            linestyles=line_styles,
            labels=tagger_labels,
            bbox_to_anchor=(0.55, 1),
        )
        histo.savefig(self.get_filename("disc", suffix))

    def plot_rocs(
        self,
        x_range: tuple[float, float] | None = None,
        resolution: int = 100,
        suffix: str | None = None,
        **roc_kwargs,
    ):
        """Plots rocs.

        Parameters
        ----------
        x_range : tuple, optional
            x-axis range, by default None
        resolution : int, optional
            number of points to use for the x-axis, by default 100
        suffix : str, optional
            suffix to add to output file name, by default None
        roc_kwargs: dict, optional
            key word arguments being passed to `RocPlot`
        """
        if x_range is None:
            x_range = (0.5, 1.0)
        sig_effs = np.linspace(*x_range, resolution)
        roc_plot_args = {
            "n_ratio_panels": len(self.backgrounds),
            "ylabel": "Background rejection",
            "xlabel": self.signal.eff_str,
            "atlas_first_tag": self.atlas_first_tag,
            "atlas_second_tag": self.atlas_second_tag,
            "y_scale": 1.3,
        }
        if roc_kwargs is not None:
            roc_plot_args.update(roc_kwargs)
        plot_roc = RocPlot(**roc_plot_args)

        for tagger in self.taggers.values():
            discs = tagger.discriminant(self.signal)
            for background in self.backgrounds:
                rej = calc_rej(
                    discs[tagger.is_flav(self.signal)],
                    discs[tagger.is_flav(background)],
                    sig_effs,
                )
                plot_roc.add_roc(
                    Roc(
                        sig_effs,
                        rej,
                        n_test=tagger.n_jets(background),
                        rej_class=background,
                        signal_class=self.signal,
                        label=tagger.label,
                        colour=tagger.colour,
                    ),
                    reference=tagger.reference,
                )

        # setting which flavour rejection ratio is drawn in which ratio panel
        for i, background in enumerate(self.backgrounds):
            plot_roc.set_ratio_class(i + 1, background)

        plot_roc.draw()
        plot_name = self.get_filename("roc", suffix)
        plot_roc.savefig(plot_name)

    def plot_var_perf(  # pylint: disable=too-many-locals
        self,
        suffix: str | None = None,
        xlabel: str = r"$p_{T}$ [GeV]",
        perf_var: str = "pt",
        h_line: float | None = None,
        working_point: float | None = None,
        disc_cut: float | None = None,
        **kwargs,
    ):
        """Variable vs efficiency/rejection plot.

        You can choose between different modes: "sig_eff", "bkg_eff", "sig_rej",
        "bkg_rej"

        Parameters
        ----------
        suffix : str, optional
            suffix to add to output file name, by default None
        xlabel : regexp, optional
            _description_, by default "$p_{T}$ [GeV]"
        perf_var: str, optional
            The x axis variable, default is 'pt'
        h_line : float, optional
            draws a horizonatal line in the signal efficiency plot
        working_point: float, optional
            The working point to use for the plot, either this, OR disc_cut must be
            set, but not both. Default is None
        disc_cut: float, optional
            The cut on the discriminant to use for the plot, either this, OR
            working_point must be set, but not both. Default is None
        **kwargs : kwargs
            key word arguments for `puma.VarVsEff`
        """
        if all([working_point, disc_cut]):
            raise ValueError("Only one of working_point or disc_cut can be set")
        if not any([working_point, disc_cut]):
            raise ValueError("Either working_point or disc_cut must be set")
        # define the curves
        plot_sig_eff = VarVsEffPlot(
            mode="sig_eff",
            ylabel=self.signal.eff_str,
            xlabel=xlabel,
            logy=False,
            atlas_first_tag=self.atlas_first_tag,
            atlas_second_tag=self.atlas_second_tag,
            n_ratio_panels=1,
            y_scale=1.4,
        )
        plot_sig_eff.apply_modified_atlas_second_tag(
            self.signal,
            working_point=working_point,
            disc_cut=disc_cut,
            flat_per_bin=kwargs.get("flat_per_bin", False),
        )
        plot_bkg = []
        for background in self.backgrounds:
            plot_bkg.append(
                VarVsEffPlot(
                    mode="bkg_rej",
                    ylabel=background.rej_str,
                    xlabel=xlabel,
                    logy=False,
                    atlas_first_tag=self.atlas_first_tag,
                    atlas_second_tag=self.atlas_second_tag,
                    n_ratio_panels=1,
                    y_scale=1.4,
                )
            )
            plot_bkg[-1].apply_modified_atlas_second_tag(
                self.signal,
                working_point=working_point,
                disc_cut=disc_cut,
                flat_per_bin=kwargs.get("flat_per_bin", False),
            )

        for tagger in self.taggers.values():
            discs = tagger.discriminant(self.signal)
            is_signal = tagger.is_flav(self.signal)

            assert perf_var in tagger.perf_vars, f"{perf_var} not in tagger {tagger.name} data!"

            plot_sig_eff.add(
                VarVsEff(
                    x_var_sig=tagger.perf_vars[perf_var][is_signal],
                    disc_sig=discs[is_signal],
                    label=tagger.label,
                    colour=tagger.colour,
                    working_point=working_point,
                    disc_cut=disc_cut,
                    **kwargs,
                ),
                reference=tagger.reference,
            )
            for i, background in enumerate(self.backgrounds):
                is_bkg = tagger.is_flav(background)
                plot_bkg[i].add(
                    VarVsEff(
                        x_var_sig=tagger.perf_vars[perf_var][is_signal],
                        disc_sig=discs[is_signal],
                        x_var_bkg=tagger.perf_vars[perf_var][is_bkg],
                        disc_bkg=discs[is_bkg],
                        label=tagger.label,
                        colour=tagger.colour,
                        working_point=working_point,
                        disc_cut=disc_cut,
                        **kwargs,
                    ),
                    reference=tagger.reference,
                )

        plot_sig_eff.draw()
        if h_line:
            plot_sig_eff.draw_hline(h_line)

        plot_base = "profile_flat_per_bin" if kwargs.get("flat_per_bin") else "profile_fixed_cut"
        wp_disc = (f"disc_cut_{disc_cut}" if disc_cut else f"wp_{working_point}").replace(".", "p")

        plot_details = f"{self.signal}_eff_vs_{perf_var}_{wp_disc}_"
        plot_suffix = f"{suffix}_" if suffix else ""
        plot_sig_eff.savefig(self.get_filename(plot_details + plot_base, plot_suffix))

        for i, background in enumerate(self.backgrounds):
            plot_bkg[i].draw()
            plot_details = f"{background}_rej_vs_{perf_var}_{wp_disc}_"
            plot_bkg[i].savefig(self.get_filename(plot_details + plot_base, plot_suffix))

    def plot_flat_rej_var_perf(
        self,
        fixed_rejections: dict[Flavour, float],
        suffix: str | None = None,
        xlabel: str = r"$p_{T}$ [GeV]",
        perf_var: str = "pt",
        h_line: float | None = None,
        **kwargs,
    ):
        """Plot signal efficiency as a function of a variable, with a fixed
        background rejection for each bin.

        Parameters
        ----------
        fixed_rejections : dict[Flavour, float]
            A dictionary of the fixed background rejections for each flavour, eg:
            fixed_rejections = {'cjets' : 0.1, 'ujets' : 0.01}
        suffix : str, optional
            suffix to add to output file name, by default None
        xlabel : regexp, optional
            _description_, by default "$p_{T}$ [GeV]"
        perf_var: str, optional
            The x axis variable, default is 'pt'
        h_line : float, optional
            draws a horizonatal line in the signal efficiency plot
        **kwargs : kwargs
            key word arguments for `puma.VarVsEff`
        """
        assert all(
            b.name in fixed_rejections for b in self.backgrounds
        ), "Not all backgrounds have a fixed rejection"
        if "disc_cut" in kwargs:
            raise ValueError("disc_cut should not be set for this plot")
        if "working_point" in kwargs:
            raise ValueError("working_point should not be set for this plot")
        plot_bkg = []
        for background in self.backgrounds:
            modified_second_tag = (
                f"{self.atlas_second_tag}\nFlat {background.rej_str} of"
                f" {fixed_rejections[background.name]} per bin"
            )
            plot_bkg.append(
                VarVsEffPlot(
                    mode="bkg_eff_sig_err",
                    ylabel=self.signal.eff_str,
                    xlabel=xlabel,
                    logy=False,
                    atlas_first_tag=self.atlas_first_tag,
                    atlas_second_tag=modified_second_tag,
                    n_ratio_panels=1,
                    y_scale=1.4,
                )
            )
        for tagger in self.taggers.values():
            discs = tagger.discriminant(self.signal)
            is_signal = tagger.is_flav(self.signal)
            for i, background in enumerate(self.backgrounds):
                is_bkg = tagger.is_flav(background)

                assert perf_var in tagger.perf_vars, f"{perf_var} not in tagger {tagger.name} data!"

                # We want x bins to all have the same background rejection, so we
                # select the plot mode as 'bkg_eff', and then treat the signal as
                # the background here. I.e, the API plots 'bkg_eff' on the y axis,
                # while keeping the 'sig_eff' a flat rate on the x axis, we therefore
                # pass the signal as the background, and the background as the
                # signal.
                plot_bkg[i].add(
                    VarVsEff(
                        x_var_sig=tagger.perf_vars[perf_var][is_bkg],
                        disc_sig=discs[is_bkg],
                        x_var_bkg=tagger.perf_vars[perf_var][is_signal],
                        disc_bkg=discs[is_signal],
                        label=tagger.label,
                        colour=tagger.colour,
                        working_point=1 / fixed_rejections[background.name],
                        flat_per_bin=True,
                        **kwargs,
                    ),
                    reference=tagger.reference,
                )

        plot_suffix = f"_{suffix}" if suffix else ""
        for i, background in enumerate(self.backgrounds):
            plot_bkg[i].draw()
            if h_line:
                plot_bkg[i].draw_hline(h_line)
            plot_details = f"{self.signal}_eff_vs_{perf_var}_"
            plot_base = (
                f"profile_flat_{background}_{int(fixed_rejections[background.name])}_rej_per_bin"
            )
            plot_bkg[i].savefig(self.get_filename(plot_details + plot_base, plot_suffix))

    def plot_fraction_scans(
        self,
        suffix: str | None = None,
        efficiency: float = 0.7,
        rej: bool = False,
        optimal_fc: bool = False,
        **kwargs,
    ):
        """Produce fraction scan (fc/fb) iso-efficiency plots.

        Parameters
        ----------
        suffix : str, optional
            suffix to add to output file name, by default None
        efficiency : float, optional
            signal efficiency, by default 0.7
        rej : bool, optional
            if True, plot rejection instead of efficiency, by default False
        optimal_fc : bool, optional
            if True, plot optimal fc/fb, by default False
        **kwargs
            Keyword arguments for `puma.Line2DPlot
        """
        if self.signal not in {Flavours.bjets, Flavours.cjets}:
            raise ValueError("Signal flavour must be bjets or cjets")
        if len(self.backgrounds) != 2:
            raise ValueError("Only two background flavours are supported")

        frac = "fc" if self.signal == Flavours.bjets else "fb"

        # set defaults
        if "logx" not in kwargs:
            kwargs["logx"] = True
        if "logy" not in kwargs:
            kwargs["logy"] = True

        fxs = fraction_scan.get_fx_values(resolution=kwargs.pop("resolution", 100))
        tag = self.atlas_second_tag + "\n" if self.atlas_second_tag else ""
        tag += f"{self.signal.eff_str} = {efficiency:.0%}"
        plot = Line2DPlot(atlas_second_tag=tag, **kwargs)
        eff_or_rej = calc_eff if not rej else calc_rej
        colours = get_good_colours()
        for i, tagger in enumerate(self.taggers.values()):
            xs = np.zeros(len(fxs))
            ys = np.zeros(len(fxs))
            sig_idx = tagger.is_flav(self.signal)
            bkg_1_idx = tagger.is_flav(self.backgrounds[0])
            bkg_2_idx = tagger.is_flav(self.backgrounds[1])
            for j, fx in enumerate(fxs):
                disc = tagger.discriminant(self.signal, fxs={frac: fx})
                xs[j] = eff_or_rej(disc[sig_idx], disc[bkg_1_idx], efficiency)
                ys[j] = eff_or_rej(disc[sig_idx], disc[bkg_2_idx], efficiency)

            # add curve for this tagger
            tagger_fx = tagger.fxs["fc" if self.signal == Flavours.bjets else "fb"]
            plot.add(
                Line2D(
                    x_values=xs,
                    y_values=ys,
                    label=f"{tagger.label} ($f_x={tagger_fx}$)",
                    colour=tagger.colour if tagger.colour else colours[i],
                )
            )

            # Add a marker for the just added fraction scan
            # The is_marker bool tells the plot that this is a marker and not a line
            fx_idx = np.argmin(np.abs(fxs - tagger_fx))
            plot.add(
                Line2D(
                    x_values=xs[fx_idx],
                    y_values=ys[fx_idx],
                    marker="x",
                    markersize=15,
                    markeredgewidth=2,
                    colour=tagger.colour,
                ),
                is_marker=True,
            )
            if optimal_fc:
                opt_idx, opt_fc = fraction_scan.get_optimal_fc(
                    np.stack((xs, ys), axis=1), fc_space=fxs, rej=rej
                )
                plot.add(
                    Line2D(
                        x_values=xs[opt_idx],
                        y_values=ys[opt_idx],
                        marker="x",
                        markersize=15,
                        markeredgewidth=1,
                        label=f"Optimal $f_x={opt_fc:.2f}$",
                    ),
                    is_marker=True,
                )
            # Adding labels
            if not rej:
                plot.xlabel = self.backgrounds[0].eff_str
                plot.ylabel = self.backgrounds[1].eff_str
            else:
                plot.xlabel = self.backgrounds[0].rej_str
                plot.ylabel = self.backgrounds[1].rej_str
        # Draw and save the plot
        plot.draw()
        plot.savefig(self.get_filename("fraction_scan", suffix))

    def make_plot(self, plot_type, kwargs):
        """Make a plot.

        Parameters
        ----------
        plot_type : str
            Type of plot
        kwargs : dict
            Keyword arguments for the plot
        """
        if plot_type not in self.plot_funcs:
            raise ValueError(f"Unknown plot type {plot_type}, choose from {self.plot_funcs.keys()}")
        self.plot_funcs[plot_type](**kwargs)
