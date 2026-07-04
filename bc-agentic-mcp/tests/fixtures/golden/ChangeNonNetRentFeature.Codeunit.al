namespace Zig.Foundation.FeatureManagement;

using System.Environment;
using Zig.System.FeatureManagement;

#pragma warning disable AL0432
codeunit 11234919 ChangeNonNetRentFeature implements FeatureV2SAN, FeatureV3SAN
#pragma warning restore AL0432
{
    var
        AutoActivateVersionLbl: Label 'Never', Comment = 'DevOps233618';
        DescriptionLbl: Label 'Change non net rent elements with keeping rental justification and authorization of rent price mutation', Comment = 'DevOps96995';
        LearnMoreLinkLbl: Label 'https://operations-docs.zig365.nl/functionaliteit/handleidingen-zig365-operations/verhuren/wonen-basis-en-instellingen/#toc_34_Handmatig_wijzigen_aanbiedingscontract', Locked = true;
        MajorReleaseOnlineLbl: Label 'R21', Locked = true;
        MajorReleaseOnPremLbl: Label 'R22', Locked = true;
        VersionAvailableOnlineLbl: Label '2112', Locked = true;
        VersionAvailableOnPremLbl: Label '2207', Locked = true;

    procedure VersionAvailableOnline(): Text[4]
    begin
        exit(VersionAvailableOnlineLbl);
    end;

    procedure VersionAvailableOnPrem(): Text[4]
    begin
        exit(VersionAvailableOnPremLbl);
    end;

    procedure GetEmpireMajorRelease(): Text[3]
    var
        Environment: Codeunit "Environment Information";
    begin
        if (Environment.IsSaaS() or Environment.IsSaaSInfrastructure()) then
            exit(MajorReleaseOnlineLbl)
        else
            exit(MajorReleaseOnPremLbl);
    end;

    procedure GetCustomerImpact(): Enum FeatureImpactSAN
    begin
        exit(FeatureImpactSAN::L1);
    end;

    procedure IsImplemented(): Boolean
    begin
        if IsActive() then
            exit(true);
    end;

    procedure IsActive(): Boolean
    var
        FeatureMgt: Codeunit FeatureMgtSAN;
    begin
        exit(FeatureMgt.IsActivated(FeatureSAN::ChangeNonNetRentWithinProposalRent));
    end;

    procedure ImplementFeature() ReloadFeatureList: Boolean
    begin
    end;

    procedure RevertImplementedFeature() ReloadFeatureList: Boolean
    var
        FeatureMgt: Codeunit FeatureMgtSAN;
    begin
        FeatureMgt.DeactivateFeature(FeatureSAN::ChangeNonNetRentWithinProposalRent);
        exit(true);
    end;

    procedure GetDescription(): Text
    begin
        exit(DescriptionLbl);
    end;

    procedure HideFeature(): Boolean
    begin
        exit(false);
    end;

    procedure IsReversible(): Boolean
    begin
        exit(true);
    end;

    procedure CheckDeactivationRequirements(): Text
    begin
        exit('');
    end;

    procedure ActivateAutomatically(): Boolean
    begin
    end;

    procedure AutoActivateVersion(): Text[10]
    begin
        exit(AutoActivateVersionLbl);
    end;

    procedure LearnMoreLink(): Text[2048]
    begin
        exit(LearnMoreLinkLbl);
    end;
}